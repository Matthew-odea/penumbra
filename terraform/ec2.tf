# ── Latest Amazon Linux 2023 AMI ───────────────────────────────────────────

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ── Security group ──────────────────────────────────────────────────────────

resource "aws_security_group" "penumbra" {
  name        = "penumbra"
  description = "Penumbra pipeline + dashboard"

  # Dashboard — public HTTP access on port 8000
  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Dashboard"
  }

  # All outbound (Polymarket WebSocket, Bedrock API, ECR, SSM, Tavily, Alchemy)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Note: no port 22 — access via SSM Session Manager only
}

# ── EC2 instance ────────────────────────────────────────────────────────────

resource "aws_instance" "penumbra" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "t3.micro"
  iam_instance_profile   = aws_iam_instance_profile.ec2.name
  vpc_security_group_ids = [aws_security_group.penumbra.id]

  # Root volume — OS + Docker layers
  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    aws_region      = var.aws_region
    ecr_registry    = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
    ecr_repo        = aws_ecr_repository.penumbra.name
    log_group       = aws_cloudwatch_log_group.penumbra.name
  })

  # Replace instance (not in-place update) when user_data changes.
  # The data EBS volume is separate, so DuckDB is safe.
  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "penumbra"
  }
}

# ── Separate EBS data volume for DuckDB ────────────────────────────────────
# Lives independently of the instance — survives terraform destroy/replace.

resource "aws_ebs_volume" "data" {
  availability_zone = aws_instance.penumbra.availability_zone
  size              = 20
  type              = "gp3"
  encrypted         = true

  tags = {
    Name = "penumbra-data"
  }

  # Never destroy this volume with terraform destroy — it holds the DB.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "data" {
  device_name  = "/dev/xvdf"
  volume_id    = aws_ebs_volume.data.id
  instance_id  = aws_instance.penumbra.id
  force_detach = true
}

# ── Elastic IP ──────────────────────────────────────────────────────────────
# Free while attached to a running instance.

resource "aws_eip" "penumbra" {
  instance = aws_instance.penumbra.id
  domain   = "vpc"

  tags = {
    Name = "penumbra"
  }
}

# ── CloudWatch log group ────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "penumbra" {
  name              = "/penumbra/sentinel"
  retention_in_days = 30
}
