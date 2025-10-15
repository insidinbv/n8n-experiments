## Intro
Small API stub, can be run in 2 modes
- "spec", if swagger file is specified: accepts requests & validates according to Swagger API spec.
- "stub", accepting anything you throw at it.

Allows "chaos-mode", randomly throwing HTTP response error codes at occasion to allow to test exception flows.

See app.python for configuration, use .env file for configuration. 

## Install
python3 -m venv .venv

source .venv/bin/activate


python -m pip install --upgrade pip
pip install -r requirements.txt

## Start stub
Set .env file for initial settings (chaos mode on, spec specified) and run, e.g. : 

Command: uvicorn app:app --host 0.0.0.0 --port 8888 --env-file .env

Watch console log and logs folder.
