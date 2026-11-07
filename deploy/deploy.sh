#!/bin/bash
# Run locally: package the current git HEAD, upload to S3, install on the instance via SSM.
#   ./deploy/deploy.sh            (after `terraform apply` in deploy/terraform)
set -euo pipefail
cd "$(dirname "$0")/.."
TF=deploy/terraform
BUCKET=$(terraform -chdir=$TF output -raw bucket)
IID=$(terraform -chdir=$TF output -raw instance_id)
REGION=$(terraform -chdir=$TF output -raw region)

git archive --format=tar.gz -o /tmp/stockmining.tar.gz HEAD
aws s3 cp --region "$REGION" /tmp/stockmining.tar.gz "s3://$BUCKET/releases/stockmining.tar.gz"
[ -f config.yaml ] && aws s3 cp --region "$REGION" config.yaml "s3://$BUCKET/config/config.yaml"

echo "waiting for SSM agent on $IID ..."
for _ in $(seq 1 60); do
  s=$(aws ssm describe-instance-information --region "$REGION" \
      --filters "Key=InstanceIds,Values=$IID" --query 'InstanceInformationList[0].PingStatus' --output text)
  [ "$s" = "Online" ] && break; sleep 10
done

CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$IID" \
  --document-name AWS-RunShellScript --comment "stockmining deploy" \
  --parameters commands="[
    \"set -e\",
    \"cloud-init status --wait || true\",
    \"/snap/bin/aws s3 cp s3://$BUCKET/releases/stockmining.tar.gz /tmp/sm.tgz\",
    \"rm -rf /tmp/sm && mkdir -p /tmp/sm && tar xzf /tmp/sm.tgz -C /tmp/sm\",
    \"BUCKET=$BUCKET bash /tmp/sm/deploy/install.sh\"
  ]" --query 'Command.CommandId' --output text)

aws ssm wait command-executed --region "$REGION" --command-id "$CMD_ID" --instance-id "$IID" || true
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$IID" \
  --query '[Status,StandardOutputContent,StandardErrorContent]' --output text | tail -30
