# POC n8n push-to-api integration

## Prerequisites
- Docker
- Python

## Flow
- Picks up files from a (mounted) folder 'data' (*.json).
- Expects JSON, in format of API
- Pushs to stub API
- Adds metadata to the API call respons
- Logs the response in the /audit/log
- IF success, move input file to /processed
- ELSE, move input file to /error

## Run the API stub
See folder api-stub.

## Run 8n8 server with folders mounted
command: docker run -it --rm -p 5678:5678 -v ~/.n8n:/home/node/.n8n -v "$PWD/data:/data" -v "$PWD/processed:/processed" -v "$PWD/error:/error" -v "$PWD/audit:/audit" -v "$PWD/workflows:/workflows" --name n8n n8nio/n8n

Todo: Standard db is internal sql lite, test to link to a Postgresql / RDS ?

## Import the push-to-api workflow
File workflows/push-to-api.json contains the n8n workflow; tailor this for the api endpoint.

Load the workflow push-to-api.json. 

command: docker exec -u node -it n8n n8n import:workflow --input=/workflows/push-to-api.json

Open http://http://localhost:5678 in browser to check imported workflow.

## Manual execution: execute a workflow in n8n with id "push-to-api": 
- Runs once, checks for files with the "Manual Trigger"
- command: docker exec -u node -it n8n n8n execute --id push-to-api
- returns extensive, detailed execution information

## Continuous execution: activates a workflow in n8n with id "push-to-api": 
(De)Activate a workflow:
- Runs continuously, checks for files with the "Local File Trigger"
- command: docker exec -u node -it n8n n8n update:workflow --id=push-to-api --active=true
- TODO check: extensive, detailed execution information in SQLlite?

