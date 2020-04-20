#!/usr/bin/env python

import boto3
import click
import glob
import os
import re
import tarfile
import time
import yaml

@click.command()
@click.option("-p", "--job-prefix", type=str, required=True,
              help="A short but descriptive string to use as an identifier for the job run")
@click.option("-c", "--config", "config_file", envvar="CONFIG_PATH", type=click.Path(exists=True), required=True,
              help="configuration file for this run")
@click.option("-j", "--num-jobs", "num_jobs", type=click.IntRange(min=1), required=True,
              help="total number of jobs to run in this batch")
@click.option("-s", "--sims-per-job", "sims_per_job", type=click.IntRange(min=1), required=True,
              help="how many sims each job should run")
@click.option("-t", "--dvc-target", "dvc_target", type=click.Path(exists=True), required=True,
              help="name of the .dvc file that is the last step in the pipeline")
@click.option("-i", "--s3-input-bucket", "s3_input_bucket", type=str, default="idd-input-data-sets")
@click.option("-o", "--s3-output-bucket", "s3_output_bucket", type=str, default="idd-pipeline-results")
@click.option("-d", "--job-definition", "batch_job_definition", type=str, default="Batch-CovidPipeline-Job")
@click.option("-q", "--job-queue", "batch_job_queue", type=str, default="Batch-CovidPipeline")
def launch_batch(job_prefix, config_file, num_jobs, sims_per_job, dvc_target, s3_input_bucket, s3_output_bucket, batch_job_definition, batch_job_queue):

    # A unique name for this job run, based on the job prefix and current time
    job_name = "%s-%d" % (job_prefix, int(time.time()))
    print("Preparing to run job: %s" % job_name)

    print("Verifying that dvc target is up to date...")
    exit_code, output = subprocess.getstatusoutput("dvc status")
    if exit_code != 0:
        print("dvc status is not up to date...")
        print(output)
        return 1

    # Update and save the config file with the number of sims to run
    print("Updating config file %s to run %d simulations..." % (config_file, sims_per_job))
    config = open(config_file).read()
    config = re.sub("nsimulations: \d+", "nsimulations: %d" % sims_per_job, config)
    with open(config_file, "w") as f:
        f.write(config)

    # Prepare to tar up the current directory, excluding any dvc outputs, so it
    # can be shipped to S3
    dvc_outputs = get_dvc_outputs()
    tarfile_name = "%s.tar.gz" % job_name
    tar = tarfile.open(tarfile_name, "w:gz")
    for p in os.listdir('.'):
        if not (p.startswith(".") or p.endswith("tar.gz") or p in dvc_outputs or p == "batch"):
            tar.add(p)
    tar.close()
 
    # Upload the tar'd contents of this directory and the runner script to S3 
    runner_script_name = "%s-runner.sh" % job_name
    s3_client = boto3.client('s3')
    s3_client.upload_file("batch/runner.sh", s3_input_bucket, runner_script_name)
    s3_client.upload_file(tarfile_name, s3_input_bucket, tarfile_name)
    os.remove(tarfile_name)

    # Prepare and launch the num_jobs via AWS Batch.
    model_data_path = "s3://%s/%s" % (s3_input_bucket, tarfile_name)
    results_path = "s3://%s/%s" % (s3_output_bucket, job_name)
    env_vars = [
            {"name": "CONFIG_PATH", "value": config_file},
            {"name": "S3_MODEL_DATA_PATH", "value": model_data_path},
            {"name": "DVC_TARGET", "value": dvc_target},
            {"name": "DVC_OUTPUTS", "value": " ".join(dvc_outputs)},
            {"name": "S3_RESULTS_PATH", "value": results_path}
    ]
    s3_cp_run_script = "aws s3 cp s3://%s/%s $PWD/run-covid-pipeline" % (s3_input_bucket, runner_script_name)
    command = ["sh", "-c", "%s; /bin/bash $PWD/run-covid-pipeline" % s3_cp_run_script]
    container_overrides = {
            'vcpus': 72,
            'memory': 184000,
            'environment': env_vars,
            'command': command
    }

    batch_client = boto3.client('batch')
    if num_jobs > 1:
        resp = batch_client.submit_job(
                jobName=job_name,
                jobQueue=batch_job_queue,
                arrayProperties={'size': num_jobs},
                jobDefinition=batch_job_definition,
            containerOverrides=container_overrides)
    else:
        resp = batch_client.submit_job(
                jobName=job_name,
                jobQueue=batch_job_queue,
                jobDefinition=batch_job_definition,
                containerOverrides=container_overrides)

    # TODO: record batch job info to a file so it can be tracked

    return 0


def get_dvc_outputs():
    ret = []
    for dvc_file in glob.glob("*.dvc"):
        with open(dvc_file) as df:
            d = yaml.load(df, Loader=yaml.FullLoader)
            if 'cmd' in d and 'outs' in d:
                ret.extend([x['path'] for x in d['outs']])
    return ret


if __name__ == '__main__':
    launch_batch()