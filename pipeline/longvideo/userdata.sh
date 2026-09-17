#!/bin/bash
# Long-video clone user-data: pull the latest boot script from S3 (hotfix channel that avoids
# re-baking the 4 TB AMI), then hand off to it as ubuntu.
#
# mkdir first: the AMI is ue-campaign-20260818-1215, which predates longvideo/ entirely, so the
# directory this writes into does not exist on a fresh clone. -H so the aws CLI reads ubuntu's
# credentials - this instance family carries them in the home directory, not an IAM role.
L=/home/ubuntu/WM-Unreal-data-collection/local_run/pipeline/longvideo
sudo -u ubuntu -H mkdir -p "$L"
sudo -u ubuntu -H aws s3 cp s3://pan-simworld/ue-revist-long-video/_ops/boot.sh "$L/boot.sh"
chmod +x "$L/boot.sh"
sudo -u ubuntu -i bash "$L/boot.sh" &
