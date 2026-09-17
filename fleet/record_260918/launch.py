"""Launch queue workers from the all-map image. One instance per call index; capacity decides.

New instances, not restarts: shutdown terminates them, so the 4 TB root volume goes away with the
instance instead of sitting on the bill after the queue drains.
"""
import json, sys, time, base64, shlex
from pathlib import Path
import boto3, botocore
F = Path('/home/ubuntu/ue_record_fleet_20260918')
AMI = 'ami-0056a81f740e03d99'; TYPE = 'g6.4xlarge'; KEY = 'Stockholm-pan-data'; SG = 'sg-00067e01b6655e01b'
SUBNETS = {'a': 'subnet-07533226d7f2f6a60', 'b': 'subnet-091e3e174ae078c27'}
BUDGET_H = 72
ec = boto3.Session(region_name='eu-north-1').client('ec2')
bm = json.loads((F / 'bundle_manifest.json').read_text())
pub = Path('/home/ubuntu/.ssh/id_ed25519.pub').read_text().strip()

def userdata(w):
    return f'''#!/bin/bash
set -uo pipefail
F=/home/ubuntu/ue_record_fleet_20260918
mkdir -p "$F" /home/ubuntu/.ssh; chmod 700 /home/ubuntu/.ssh
grep -qxF {shlex.quote(pub)} /home/ubuntu/.ssh/authorized_keys || printf '%s\\n' {shlex.quote(pub)} >> /home/ubuntu/.ssh/authorized_keys
chmod 600 /home/ubuntu/.ssh/authorized_keys; chown -R ubuntu:ubuntu "$F" /home/ubuntu/.ssh
systemd-run --unit=ue-newroute-budget --on-active={BUDGET_H}h /sbin/shutdown -h now
sudo -u ubuntu -H aws --region eu-north-1 s3 cp s3://pan-simworld/{bm["key"]} "$F/bundle.tar.gz" --only-show-errors
printf '%s  %s\\n' '{bm["sha256"]}' "$F/bundle.tar.gz" | sha256sum -c - || {{ shutdown -h +2; exit 1; }}
sudo -u ubuntu -H tar xzf "$F/bundle.tar.gz" -C "$F"
sudo -u ubuntu -H bash "$F/bundle/record_boot.sh" {w}
'''

def launch(i):
    w = f'{i:02}'; az = 'a' if i % 2 == 0 else 'b'
    r = ec.run_instances(ImageId=AMI, InstanceType=TYPE, MinCount=1, MaxCount=1, KeyName=KEY,
                         SubnetId=SUBNETS[az], SecurityGroupIds=[SG],
                         InstanceInitiatedShutdownBehavior='terminate',
                         BlockDeviceMappings=[{'DeviceName': '/dev/sda1', 'Ebs': {'DeleteOnTermination': True, 'VolumeSize': 4096, 'VolumeType': 'gp3'}}],
                         UserData=userdata(w),
                         TagSpecifications=[{'ResourceType': 'instance', 'Tags': [
                             {'Key': 'Name', 'Value': f'ue-revalidate-{w}'},
                             {'Key': 'RecordRun', 'Value': '260918'},
                             {'Key': 'RecordWorker', 'Value': w}]}])
    iid = r['Instances'][0]['InstanceId']
    (F / f'launch_{w}.json').write_text(json.dumps({'worker': w, 'instance': iid, 'az': az, 'epoch': time.time()}, indent=1))
    return iid

if __name__ == '__main__':
    for i in map(int, sys.argv[1:]):
        try:
            print('STARTED', f'{i:02}', launch(i), flush=True)
        except botocore.exceptions.ClientError as e:
            print('CAPACITY', f'{i:02}', e.response['Error']['Code'], flush=True)
