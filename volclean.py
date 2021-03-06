#!/usr/bin/env python3
import boto3
import boto3.session
import botocore.exceptions
from datetime import datetime, timedelta, timezone
import sys
import re
import argparse
import logging
import json
import uuid
from multiprocessing.pool import ThreadPool
from pprint import pprint
from retrying import retry

logging.basicConfig(level=logging.WARN, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('__main__').setLevel(logging.INFO)
logging.getLogger('VolumeCleaner').setLevel(logging.INFO)
log = logging.getLogger(__name__)

args = None


def main(argv):
    global args
    p = argparse.ArgumentParser(description='Remove unused EBS volumes')
    p.add_argument('--access-key-id', '-k', help='AWS Access Key ID', dest='access_key_id')
    p.add_argument('--secret-access-key', '-s', help='AWS Secret Access Key', dest='secret_access_key')
    p.add_argument('--role', help='AWS IAM role', dest='role')
    p.add_argument('--account', help='AWS account', dest='account', default=None, nargs='+')
    p.add_argument('--scrape-org', help='Scrape the entire AWS Org (default: False)', dest='scrape_org',
                   action='store_true')
    p.add_argument('--region', '-r', help='AWS Region (default: all)', dest='region', type=str, default=None,
                   nargs='+')
    p.add_argument('--run-dont-ask', '-y', help='Assume YES to all questions', action='store_true', default=False,
                   dest='all_yes')
    p.add_argument('--pool-size', '-p',
                   help='Thread Pool Size - how many AWS API requests we do in parallel (default: 10)',
                   dest='pool_size', default=10, type=int)
    p.add_argument('--age', '-a', help='Days after which a Volume is considered orphaned (default: 14)', dest='age',
                   default=14, type=check_positive)
    p.add_argument('--tags', '-t', help='Tag filter in format "key:regex" (E.g. Name:^integration-test)',
                   dest='tags', type=str, default=None, nargs='+')
    p.add_argument('--ignore-metrics', '-i', help='Ignore Volume Metrics - remove all detached Volumes',
                   dest='ignore_metrics', action='store_true', default=False)
    p.add_argument('--reportfile', '-rf', help='Filename for JSON report of removed volumes', dest='report_filename',
                   type=str, default=None)
    p.add_argument('--verbose', '-v', help='Verbose logging', dest='verbose', action='store_true', default=False)
    args = p.parse_args(argv)

    if args.verbose:
        logging.getLogger('__main__').setLevel(logging.DEBUG)
        logging.getLogger('VolumeCleaner').setLevel(logging.DEBUG)

    if args.role and args.scrape_org:
        accounts = [AWSAccount(account_id, role=args.role) for account_id in get_org_accounts(filter_current_account=True)]
        accounts.append(AWSAccount(current_account_id()))
    elif args.account:
        accounts = [AWSAccount(account_id, role=args.role) for account_id in args.account]
    else:
        log.info('Account not specified, assuming default account')
        accounts = [AWSAccount(current_account_id())]

    if not args.region:
        log.info('Region not specified, assuming all regions')
        regions = all_regions(args)
    else:
        regions = args.region

    report_data = {}
    for account in accounts:
        try:
            report_data[account.account_id] = {}
            for region in regions:
                try:
                    vol_clean = VolumeCleaner(args, account=account, region=region)
                    vol_clean.run()
                    report_data[account.account_id][region] = vol_clean.removal_log
                except botocore.exceptions.ClientError as e:
                    if e.response['Error']['Code'] == 'UnauthorizedOperation':
                        log.error(
                            'Not authorized to collect resources in account {} region {}'.format(account.account_id,
                                                                                                 region))
                    else:
                        raise
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'AccessDenied':
                log.error('Not authorized to collect resources in account {}'.format(account.account_id))
            else:
                raise

    if args.report_filename:
        log.debug('Writing removal report to {}'.format(args.report_filename))
        with open(args.report_filename, 'w') as report_file:
            json.dump(report_data, report_file, sort_keys=True, indent=4)


def check_positive(value):
    ivalue = int(value)
    if ivalue <= 0:
        raise(argparse.ArgumentTypeError("%s is an invalid positive int value" % value))
    return ivalue


def all_regions(args):
    session = aws_session()
    ec2 = session.client('ec2', region_name='us-east-1')
    regions = ec2.describe_regions()
    return [r['RegionName'] for r in regions['Regions']]


def retry_on_request_limit_exceeded(e):
    if isinstance(e, botocore.exceptions.ClientError):
        if e.response['Error']['Code'] == 'RequestLimitExceeded':
            log.debug('AWS API request limit exceeded, retrying with exponential backoff')
            return True
    return False


class AWSAccount:
    def __init__(self, account_id, role=None):
        self.account_id = account_id
        self.role = role


class VolumeCleaner:
    def __init__(self, args, account: AWSAccount, region):
        self.args = args
        self.log = logging.getLogger(__name__)
        self.account = account.account_id
        self.region = region
        self.role = account.role
        self.removal_log = []

    @retry(stop_max_attempt_number=30, wait_exponential_multiplier=3000, wait_exponential_max=120000,
           retry_on_exception=retry_on_request_limit_exceeded)
    def run(self):
        p = ThreadPool(self.args.pool_size)
        try:
            candidates = list(filter(None, p.map(self.candidate, self.available_volumes())))
        finally:
            p.close()
            p.join()
        if len(candidates) > 0 and (self.args.all_yes or query_yes_no(
                'Do you want to remove {} Volumes in Region {}?'.format(len(candidates), self.region))):
            self.log.info(
                'Removing {} Volumes in Account {} Region {}'.format(len(candidates), self.account, self.region))
            p = ThreadPool(self.args.pool_size)
            try:
                p.map(self.remove_volume, candidates)
            finally:
                p.close()
                p.join()
            self.log.info('Done')
        else:
            self.log.info('Not doing anything in Account {} Region {}'.format(self.account, self.region))

    def available_volumes(self):
        self.log.debug('Finding unused Volumes in Account {} Region {}'.format(self.account, self.region))
        session = aws_session(self.account, self.role)
        ec2 = session.resource('ec2', region_name=self.region)
        volumes = ec2.volumes.filter(Filters=[{'Name': 'status', 'Values': ['available']}])
        self.log.debug(
            'Found {} unused Volumes in Account {} Region {}'.format(len(list(volumes)), self.account, self.region))
        return volumes

    # based on http://blog.ranman.org/cleaning-up-aws-with-boto3/
    def get_metrics(self, volume):
        self.log.debug('Retrieving Metrics for Volume {} in Account {} Region {}'.format(volume.volume_id, self.account,
                                                                                         self.region))
        session = aws_session(self.account, self.role)
        cw = session.client('cloudwatch', region_name=self.region)

        end_time = datetime.now() + timedelta(days=1)
        start_time = end_time - timedelta(days=self.args.age)

        return cw.get_metric_statistics(
            Namespace='AWS/EBS',
            MetricName='VolumeIdleTime',
            Dimensions=[{'Name': 'VolumeId', 'Value': volume.volume_id}],
            Period=3600,
            StartTime=start_time,
            EndTime=end_time,
            Statistics=['Minimum'],
            Unit='Seconds'
        )

    def tag_filter(self, volume):
        if not self.args.tags:
            return True

        for tag in self.args.tags:
            search_key, search_value = tag.split(':', 1)
            if not search_key or not search_value:
                raise ValueError('Malformed tag search: {}'.format(tag))

            tag_value = next((item['Value'] for item in volume.tags if item['Key'] == search_key), None)
            if tag_value is None:
                self.log.debug('Volume {} in Account {} Region {} has no tag {}'.format(volume.volume_id, self.account,
                                                                                        self.region, search_key))
                return False

            regex = re.compile(search_value)
            if not regex.search(tag_value):
                self.log.debug(
                    "Volume {} in Account {} Region {} with tag {}={} doesn't match regex {}".format(volume.volume_id,
                                                                                                     self.account,
                                                                                                     self.region,
                                                                                                     search_key,
                                                                                                     tag_value,
                                                                                                     search_value))
                return False

        return True

    def candidate(self, volume):
        if not self.tag_filter(volume):
            self.log.debug(
                'Volume {} in Account {} Region {} is no candidate for deletion'.format(volume.volume_id, self.account,
                                                                                        self.region))
            return None

        if self.args.ignore_metrics:
            self.log.debug(
                'Volume {} in Account {} Region {} is a candidate for deletion'.format(volume.volume_id, self.account,
                                                                                       self.region))
            return volume

        metrics = self.get_metrics(volume)
        if len(metrics['Datapoints']) == 0:
            now = datetime.utcnow().replace(tzinfo=timezone.utc)
            expire = volume.create_time + timedelta(days=self.args.age)
            if now >= expire:
                self.log.debug('Volume {} in Account {} Region {} has no metrics yet but is older than {} days, so is a candidate for deletion'.format(volume.volume_id,
                                                                                                        self.account,
                                                                                                        self.region,
                                                                                                        self.args.age))
                return volume
            else:
                self.log.debug('Volume {} in Account {} Region {} has no metrics yet and is no candidate for deletion'.format(volume.volume_id,
                                                                                                        self.account,
                                                                                                        self.region))
                return None

        for metric in metrics['Datapoints']:
            if metric['Minimum'] < 299:
                self.log.debug('Volume {} in Account {} Region {} is no candidate for deletion'.format(volume.volume_id,
                                                                                                       self.account,
                                                                                                       self.region))
                return None

        self.log.debug(
            'Volume {} in Account {} Region {} is a candidate for deletion'.format(volume.volume_id, self.account,
                                                                                   self.region))
        return volume

    @retry(stop_max_attempt_number=100, wait_exponential_multiplier=1000, wait_exponential_max=30000,
           retry_on_exception=retry_on_request_limit_exceeded)
    def remove_volume(self, volume, thread_safe=True):
        if thread_safe:
            session = aws_session(self.account, self.role)
            ec2 = session.resource('ec2', region_name=self.region)
            volume = ec2.Volume(volume.volume_id)

        self.log.debug(
            'Removing Volume {} in Account {} Region {} with size {} GiB created on {}'.format(volume.volume_id,
                                                                                               self.account,
                                                                                               self.region, volume.size,
                                                                                               volume.create_time))
        removal_log_record = {'volume_id': volume.volume_id,
                              'volume_type': volume.volume_type,
                              'size': volume.size,
                              'create_time': str(volume.create_time),
                              'removal_time': '{}+00:00'.format(datetime.utcnow())}
        volume.delete()
        self.removal_log.append(removal_log_record)


# From http://stackoverflow.com/questions/3041986/apt-command-line-interface-like-yes-no-input
def query_yes_no(question, default='no'):
    valid = {"yes": True, "y": True, "ye": True,
             "no": False, "n": False}
    if default is None:
        prompt = " [y/n] "
    elif default == "yes":
        prompt = " [Y/n] "
    elif default == "no":
        prompt = " [y/N] "
    else:
        raise ValueError("invalid default answer: '%s'" % default)

    while True:
        sys.stdout.write(question + prompt)
        choice = input().lower()
        if default is not None and choice == '':
            return valid[default]
        elif choice in valid:
            return valid[choice]
        else:
            sys.stdout.write("Please respond with 'yes' or 'no' "
                             "(or 'y' or 'n').\n")


def current_account_id():
    session = aws_session()
    return session.client('sts').get_caller_identity().get('Account')


def get_org_accounts(filter_current_account=False):
    session = aws_session()
    client = session.client('organizations')
    accounts = []
    try:
        response = client.list_accounts()
        accounts = response.get('Accounts', [])
        while response.get('NextToken') is not None:
            response = client.list_accounts(NextToken=response['NextToken'])
            accounts.extend(response.get('Accounts', []))
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'AccessDeniedException':
            log.error('AWS error - missing permissions to list organization accounts')
        else:
            raise
    filter_account_id = current_account_id() if filter_current_account else -1
    accounts = [aws_account['Id'] for aws_account in accounts if aws_account['Id'] != filter_account_id]
    for account in accounts:
        log.debug('AWS found org account {}'.format(account))
    log.info('AWS found a total of {} org accounts'.format(len(accounts)))
    return accounts


def aws_session(aws_account=None, aws_role=None):
    if aws_role and aws_account:
        role_arn = 'arn:aws:iam::{}:role/{}'.format(aws_account, aws_role)
        session = boto3.session.Session(aws_access_key_id=args.access_key_id,
                                        aws_secret_access_key=args.secret_access_key,
                                        region_name='us-east-1')
        sts = session.client('sts')
        token = sts.assume_role(RoleArn=role_arn,
                                RoleSessionName='{}-{}'.format(aws_account, str(uuid.uuid4())))
        credentials = token["Credentials"]
        return boto3.session.Session(aws_access_key_id=credentials["AccessKeyId"],
                                     aws_secret_access_key=credentials["SecretAccessKey"],
                                     aws_session_token=credentials["SessionToken"])
    else:
        return boto3.session.Session(aws_access_key_id=args.access_key_id,
                                     aws_secret_access_key=args.secret_access_key)


if __name__ == "__main__":
    main(sys.argv[1:])
