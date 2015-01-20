import sys
import os
import argparse
import subprocess
import pysam
import shutil
import logging
import copy
import pybedtools
from fasta_utils import *
from vcf_utils import *
from sv_interval import SVInterval, get_gaps_file, interval_overlaps_interval_list, merge_intervals
from pindel_reader import PindelReader
from breakdancer_reader import BreakDancerReader
from cnvnator_reader import CNVnatorReader
from generate_sv_intervals import parallel_generate_sc_intervals
from run_spades import run_spades_parallel
from run_age import run_age_parallel
from generate_final_vcf import convert_metasv_bed_to_vcf

FORMAT = '%(levelname)s %(asctime)-15s %(name)-20s %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)
logger = logging.getLogger(__name__)


def create_dirs(dirlist):
    for dirname in dirlist:
        if not os.path.isdir(dirname):
            logger.info("Creating directory %s" % (dirname))
            os.makedirs(dirname)


def run_metasv(sample, reference, pindel_vcf = [], pindel_native = [], breakdancer_vcf = [], breakdancer_native = [], breakseq_vcf = [], cnvnator_vcf = [], cnvnator_native = [], gatk_vcf = [], gaps = None, filter_gaps = False, keep_standard_contigs = False,
        wiggle = 100, overlap_ratio = 0.5, workdir = "work", outdir = "out", boost_ins = False, bam = None, chromosomes = [], num_threads = 1, spades = None, age = None, disable_assembly = True):

    # Check if there is work to do
    if not (pindel_vcf + breakdancer_vcf + breakseq_vcf + cnvnator_vcf):
        logger.error("Nothing to do since no SV file specified")
        return 1

    # Create the directories for working
    bedtools_tmpdir = os.path.join(workdir, "bedtools")
    create_dirs([workdir, outdir, bedtools_tmpdir])

    # Reference handling
    if not os.path.isfile(reference + ".fai"):
        logger.error("Reference file %s is not indexed" % (reference))
        return 1

    fasta_handle = pysam.Fastafile(reference) if os.path.isfile(reference) else None
    contigs = get_contigs(reference)
    include_intervals = sorted([SVInterval(contig.name, 0, contig.length, contig.name, "include", length = contig.length) for contig in contigs])

    # Generate the list of contigs to process
    contig_whitelist = set(chromosomes) if chromosomes else set([contig.name for contig in contigs])
    if keep_standard_contigs:
        contig_whitelist &= set([str(i) for i in xrange(1, 23)] + ["chr%d" % (i) for i in xrange(1, 23)] + ["X", "Y", "MT", "chrX", "chrY", "chrM"])
    logger.info("Only SVs on the following contigs will be reported: %s" % (sorted(list(contig_whitelist))))

    # Load the intervals from different files
    vcf_name_list = [("CNVnator", cnvnator_vcf), ("Pindel", pindel_vcf), ("BreakDancer", breakdancer_vcf), ("BreakSeq", breakseq_vcf), ("HaplotypeCaller", gatk_vcf)]
    tools = []
    intervals = {}
    sv_types = set()
    
    gap_intervals = []
    if filter_gaps:
        gaps = gaps if gaps else get_gaps_file(contig_whitelist)
        gap_intervals = sorted(load_gap_intervals(gaps))
        
    pindel_lis = []
    if pindel_native is not None:
        for pindel_native_file in pindel_native:
            for pindel_record in PindelReader(pindel_native_file, fasta_handle):
                if pindel_record.sv_type == "LI":
                    interval = pindel_record.to_sv_interval()
                    if not interval_overlaps_interval_list(interval, gap_intervals) and interval.chrom in contig_whitelist:
                        pindel_lis.append(pindel_record.to_sv_interval())
                        
    for toolname, vcfname in vcf_name_list:
        if not vcfname: continue
        tools.append(toolname)
        intervals[toolname] = {}
        
        if toolname == "Pindel" and pindel_lis:
            intervals[toolname]["INS"] = pindel_lis
            sv_types |= set(["INS"])
            
        vcf_list = []
        for vcf in vcfname:
            if os.path.isdir(vcf):
                logger.info("Will load from per-chromosome VCFs from directory %s for tool %s" % (vcf, toolname))
                vcf_list += [os.path.join(vcf, "%s.vcf.gz" % (contig.name)) for contig in contigs if (not contig_whitelist or contig.name in contig_whitelist)]
            else:
                vcf_list.append(vcf)
                
        for vcf in vcf_list:
            load_intervals(vcf, intervals[toolname], gap_intervals, include_intervals, toolname, contig_whitelist, toolname == "HaplotypeCaller")
        sv_types |= set(intervals[toolname].keys())
        
    logger.info("SV types are %s" % (str(sv_types)))
    tool_merged_intervals = {}
    final_intervals = []
    
    bd_out = os.path.join(outdir, "breakdancer.vcf")
    pindel_out = os.path.join(outdir, "pindel.vcf")
    cnvnator_out = os.path.join(outdir, "cnvnator.vcf")
    breakseq_out = os.path.join(outdir, "breakseq.vcf")
    
    for toolname, tool_out in [("BreakDancer", bd_out), ("Pindel", pindel_out), ("CNVnator", cnvnator_out), ("BreakSeq", breakseq_out)]:
        if tool_out is None or toolname not in intervals: continue

        intervals_tool = []
        tool_out_fd = open(tool_out, "w")
        chr_intervals_tool = {contig.name: [] for contig in contigs}
        for sv_type in sv_types:
            if sv_type in intervals[toolname]:
                intervals_tool.extend([copy.deepcopy(interval) for interval in intervals[toolname][sv_type]])
        for interval in intervals_tool:
            interval.do_validation(overlap_ratio)
            interval.fix_pos()
            chr_intervals_tool[interval.chrom].append(interval)
        print_vcf_header(tool_out_fd, reference, contigs, sample)
        for contig in contigs:
            chr_intervals_tool[contig.name].sort()
            for interval in chr_intervals_tool[contig.name]:
                vcf_record = interval.to_vcf_record(fasta_handle)
                if vcf_record is not None:
                    tool_out_fd.write("%s\n" % (vcf_record))
        tool_out_fd.close()
        pysam.tabix_index(tool_out, force=True, preset="vcf")
        
    for sv_type in sv_types:
        logger.info("Processing SVs of type %s" % (sv_type))
        tool_merged_intervals[sv_type] = []
        for tool in tools:
            if sv_type not in intervals[tool]: continue
            logger.info("First level merging for %s for tool %s" % (sv_type, tool))
            tool_merged_intervals[sv_type] += merge_intervals(intervals[tool][sv_type])
        merged_intervals = merge_intervals(tool_merged_intervals[sv_type])
        intervals1 = []
        intervals2 = []
        for interval in tool_merged_intervals[sv_type]:
            if interval_overlaps_interval_list(interval, merged_intervals, overlap_ratio, overlap_ratio):
                intervals2.append(interval)
            else:
                intervals1.append(interval)

        final_intervals.extend(merge_intervals(intervals1) + merge_intervals(intervals2))
        
    final_chr_intervals = {contig.name: [] for contig in contigs}
    for interval in final_intervals:
        interval.do_validation(overlap_ratio)
        interval.fix_pos()
        final_chr_intervals[interval.chrom].append(interval)
        
    out_vcf = os.path.join(outdir, "metasv.vcf")
    outfd = open(out_vcf, "w")
    print_vcf_header(outfd, reference, contigs, sample)
    final_stats = {}
    
    bed_intervals = []
    merged_bed = os.path.join(workdir, "metasv.bed")
    for contig in contigs:
        final_chr_intervals[contig.name].sort()
        for interval in final_chr_intervals[contig.name]:
            vcf_record = interval.to_vcf_record(fasta_handle)
            if vcf_record is not None:
                key = (interval.sv_type, "PASS" if interval.is_validated else "LowQual", "PRECISE" if interval.is_precise else "IMPRECISE", tuple(sorted(list(interval.sources))))
                if key not in final_stats: final_stats[key] = 0
                final_stats[key] += 1
                outfd.write("%s\n" % (vcf_record))
            bed_interval = interval.to_bed_interval(sample)
            if bed_interval is not None:
                bed_intervals.append(bed_interval)
                
    pybedtools.BedTool(bed_intervals).saveas(merged_bed)
    outfd.close()
    
    for key in sorted(final_stats.keys()):
        logger.info(str(key) + ":" + str(final_stats[key]))
        
    if not disable_assembly:
        if spades is None:
            logger.error("Spades executable not specified")
            return 1
            
        if age is None:
            logger.error("AGE executable not specified")
            return 1
        
        spades_tmpdir = os.path.join(workdir, "spades")
        age_tmpdir = os.path.join(workdir, "age")

        create_dirs([spades_tmpdir, age_tmpdir])
        
        assembly_bed = merged_bed
        
        if boost_ins:
            logger.info("Generating intervals for insertions")
            assembly_bed = parallel_generate_sc_intervals([bam.name], list(contig_whitelist), merged_bed, workdir, num_threads = num_threads)
            logger.info("Generated intervals for assembly in %s" % (assembly_bed))
            
        logger.info("Will run assembly now")
        
        assembled_fasta, ignored_bed = run_spades_parallel(bam = bam.name, spades = spades, bed = assembly_bed, work = spades_tmpdir, pad = 500, nthreads = num_threads, chrs = list(contig_whitelist))
        breakpoints_bed = run_age_parallel(intervals_bed = assembly_bed, reference = reference, assembly = assembled_fasta, pad = 500, age = age, chrs = list(contig_whitelist), nthreads = num_threads, min_contig_len = 100, age_workdir = age_tmpdir)
        
        final_bed = os.path.join(workdir, "final.bed")
        if ignored_bed:
            pybedtools.BedTool(breakpoints_bed).cat(pybedtools.BedTool(ignored_bed), postmerge = False).sort().saveas(final_bed)
        else:
            pybedtools.BedTool(breakpoints_bed).saveas(final_bed)
    else:
        final_bed = merged_bed
        
    final_vcf = os.path.join(outdir, "final.vcf")
    convert_metasv_bed_to_vcf(bedfile = final_bed, vcf_out = final_vcf, sample = sample)
    
    pybedtools.cleanup(remove_all = True)
