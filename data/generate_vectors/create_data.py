import numpy as np
import multiprocessing as mp,os
import warnings
import pysam
from bam2tensor import Bam2Tensor
import traceback


parser = argparse.ArgumentParser(description='MiMSI Vector Generation Utility')
parser.add_argument('--case-list', default="", help='Case List for generating sample vectors in bulk, if specified all other input file args will be ignored')
parser.add_argument('--tumor-bam', default="test-001-Tumor.bam", help='Tumor bam file for conversion')
parser.add_argument('--normal-bam', default="test-001-Normal.bam", help='Matched normal bam file for conversion')
parser.add_argument('--case-id', default="test-001", help='Unique identifier for the sample/case')
parser.add_argument('--microsatellites-list', default="./microsatellites.list", help='The list of microsatellites to check in the tumor/normal pair')
parser.add_argument('--save-location', default="../generated_samples", help='The location on the filesystem to save the converted vectors')
parser.add_argument('--is-labeled', default=False, help="Indicated whether or not the data provided is labeled")
parser.add_argument('--coverage', default=50, help="Required coverage for both the tumor and the normal. Any coverage in excess of this limit will be randomly downsampled")
parser.add_argument('--cores', default=16, help="Number of cores to utilize in parallel")
args = parser.parse_args()


'''
    Process is the most granular conversion function. It converts a single 
    microsatellite into a 100 x L x 3 vector, where 100 is the downsampled
    coverage configured in Bam2Tensor. If the microsatellite does not meet
    the required thresholds for length, repeat unit length, number of repeats
    or coverage (None, None) is returned. Otherwise a tuple containing 
    the vector and its location is returned to the wrapper function
'''
def process(line, bamfile, normalbamfile):
    vals = line.split('\t')
    if not vals[0].isdigit():
        return (None, None)

    if int(vals[2]) == 1 and int(vals[4]) < 10:
        return (None, None)

    if int(vals[2]) < 5 and int(vals[4]) < 5:
        return (None, None)

    chrom = vals[0]
    start = int(vals[1])
    end = start + int(vals[2])*int(vals[4])
    total_len = end - start
    if total_len < 5 or total_len > 40:
        return (None, None)
    tumor_test = Bam2Tensor(bamfile, normalbamfile, str(chrom), int(start), int(end), args.coverage)
    return ( tumor_test.createTensor(), [str(chrom), int(start), int(end)] )

'''
    This is a wrapper function that executes "process" above for a given
    chunk of the microsatellites list. It compiles a list of all the 
    microsatellites successfully converted to a numpy vector and returns 
    it along with the location information of the loci
'''
def process_wrapper(bam_filename, norm_filename, chunkStart, chunkSize):
    with open("microsatellites.list",'r') as ms_list:
        # only look at microsatellites assigned to this process (chunks)
        ms_list.seek(chunkStart)
        lines = ms_list.read(chunkSize).splitlines()
        
        results = []
        locations = []
        
        if lines is None:
            # return empty
            return (results, locations)
        
        bamfile = pysam.AlignmentFile(bam_filename, 'rb')
        normalbamfile = pysam.AlignmentFile(norm_filename, 'rb')
         
        for line in lines:
            result, loc = process(line, bamfile, normalbamfile)
            if result is not None:
                # We got one!!!
                results.append(result)
                locations.append(loc)
        return (results, locations)

'''
    Generic helper method to break up a file into many smaller chunks,
    Here we use it to break up the microsatellites list so that we can 
    generate many different microsatellite vectors in parallel.
'''
def chunkify(fname, size=1024*1024):
    fileEnd = os.path.getsize(fname)
    with open(fname,'r') as f:
        chunkEnd = f.tell()
        while True:
            chunkStart = chunkEnd
            f.seek(size,1)
            f.readline()
            chunkEnd = f.tell()
            yield chunkStart, chunkEnd - chunkStart
            if chunkEnd > fileEnd:
                break

'''
    This is the top level function that converts an entire tumor/normal pair of bam files 
    into a vector collection that MiMSI will utilize in subsequent steps. It is setup to run
    in a parallel processing environment, with cores specified as a command line arg in main

    The steps it goes through are as follows: 
        1. chunk the list of microsatellites were interested in so that they can be executed 
            in parallel
        2. create a process_wrapper job to handle each chunk
        3. wait for all chunks to complete
        4. combine the results of each chunk
        5. close and return the combined results
'''
def convert_bam(bamfile, norm_filename):
    all_instances = []
    all_locations = []
    cores = args.cores

    pool = mp.Pool(cores)
    jobs = []

    print("creating jobs")

    try:
        #create jobs
        for chunkStart,chunkSize in chunkify(args.microsatellites_list):
            jobs.append( pool.apply_async(process_wrapper,(bamfile, norm_filename, chunkStart,chunkSize)) )

 
        #wait for all jobs to finish
        for job in jobs:
            result = job.get()
            if result is not None:
                all_instances = all_instances + result[0]
                all_locations = all_locations + result[1]
    except Exception as e:
        print("There was an exception")
        print(traceback.format_exc())
 
    #clean up
    pool.close()
    return (all_instances, all_locations)



''' 
    Save the final collection of microsatellite instances to disk to be used
    in later stages of MiMSI. Saves each sample in the following manner:

        {sample}_{label}_data.npy
        {sample}_{label}_locations.npy

    This final filename format is important for later stages of the pipeline,
    as the data loader will parse the underscore deliminated filename to determine
    the sample id and the label. Sample id does need to be unique.

'''
def save_bag(sample, label, data, locations):
    # if no instances that met the coverage threshold were found, return
    if len(data) == 0:
        print('Sample %s did not have any microsatellites above the required threshold level. \n', sample)
        return

    # zero pad all sites to the length of the longest microsatellite found
    max_cols = np.max([elem.shape[1] for elem in data])
    data = [np.concatenate((entry,np.zeros((100,max_cols-entry.shape[1],3))), axis=1) for entry in data]
    data = np.array(data)

    # save the instance to disk as it's generated, this is very important when 
    # generating a large number of samples, otherwise everything will explode when you try 
    # to keep storing all your samples in memory
    file_name = args.save_location + '/' + sample + "_" + str(label) + "_" + "data"
    loc_file_name =  args.save_location + '/' + sample + "_" + str(label) + "_" + "locations"
    np.save(file_name, data)
    np.save(loc_file_name, locations)




if __name__ == "__main__":
    
    # If a file is given use that to generate our data
    if args.case_list is not None:

        with open(args.case_list, 'r') as f:
            lines = f.read().splitlines()
            N = len(lines)
            data = []
            labels = []
            counter = 0
            chunk_counter = 1

            for line in lines:
                # Get all of our values
                vals = line.split("\t")
                sample = vals[0]
                bam_file = vals[1]
                norm_file = vals[2]
                label = -1
                if args.is_labeled:
                    label = vals[3]

                try:
                    # convert
                    result = convert_bam(bam_file, norm_file)
                    
                    data = result[0] # the converted vector
                    locations = np.array(result[1]) # the location utilized
                    
                    # save to disk
                    save_bag(sample, label, data, locations)
                    counter += 1
                    print("Finished bam file number... " + str(counter))
                
                except Exception as e:
                    print(e)
                    break

    # Otherwise we are just going to convert the given sample
    else:

        result = convert_bam(args.tumor_bam, args.normal_bam)
        data = result[0] # the converted vector
        locations = np.array(result[1]) # the location utilized
                    
        # save to disk
        save_bag(args.case_id, -1, data, locations)


