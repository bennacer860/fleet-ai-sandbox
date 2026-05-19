terraform {
  backend "s3" {
    bucket  = "polymarket-bot-tfstate-451194071227-eu-west-1"
    key     = "fleet-ai-sandbox/polymarket-bot/terraform.tfstate"
    region  = "eu-west-1"
    encrypt = true
  }
}
