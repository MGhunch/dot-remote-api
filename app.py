from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# Airtable configuration
AIRTABLE_API_KEY = os.environ.get('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID', 'appXXXXXXXXXXXXXX')

# Table URLs
PROJECTS_URL = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Projects'
CLIENTS_URL = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Clients'

def get_headers():
    return {
        'Authorization': f'Bearer {AIRTABLE_API_KEY}',
        'Content-Type': 'application/json'
    }

@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'dot-remote-api'})

@app.route('/clients', methods=['GET'])
def get_clients():
    """
    Returns list of clients from Clients table.
    Sorted alphabetically by name.
    """
    try:
        params = {
            'fields[]': ['Client code', 'Clients'],
            'sort[0][field]': 'Clients',
            'sort[0][direction]': 'asc'
        }
        
        response = requests.get(CLIENTS_URL, headers=get_headers(), params=params)
        response.raise_for_status()
        data = response.json()
        
        # Format clients
        result = []
        for record in data.get('records', []):
            fields = record.get('fields', {})
            code = fields.get('Client code', '')
            name = fields.get('Clients', '')
            
            if code and name:
                result.append({
                    'code': code,
                    'name': name
                })
        
        return jsonify(result)
    
    except requests.exceptions.RequestException as e:
        print(f"Airtable API error: {e}")
        return jsonify({'error': 'Failed to fetch clients'}), 500

@app.route('/jobs', methods=['GET'])
def get_jobs():
    """
    Returns jobs for a specific client.
    Query param: client (the client name)
    Only includes active projects (In Progress or On Hold).
    """
    client_query = request.args.get('client', '')
    
    if not client_query:
        return jsonify({'error': 'Client parameter required'}), 400
    
    try:
        # Filter by exact client match and exclude completed/archived
        filter_formula = f"AND({{Client}}='{client_query}', NOT({{Status}}='Completed'), NOT({{Status}}='Archived'))"
        
        params = {
            'filterByFormula': filter_formula,
            'fields[]': ['Job Number', 'Project Name', 'Client', 'Status', 'Stage'],
            'sort[0][field]': 'Job Number',
            'sort[0][direction]': 'asc'
        }
        
        response = requests.get(PROJECTS_URL, headers=get_headers(), params=params)
        response.raise_for_status()
        data = response.json()
        
        # Format jobs
        jobs = []
        for record in data.get('records', []):
            fields = record.get('fields', {})
            job_number = fields.get('Job Number', '')
            project_name = fields.get('Project Name', '')
            
            if job_number:
                jobs.append({
                    'id': job_number,
                    'name': f"{job_number} - {project_name}" if project_name else job_number,
                    'status': fields.get('Status', ''),
                    'stage': fields.get('Stage', ''),
                    'recordId': record.get('id', '')
                })
        
        return jsonify(jobs)
    
    except requests.exceptions.RequestException as e:
        print(f"Airtable API error: {e}")
        return jsonify({'error': 'Failed to fetch jobs'}), 500

@app.route('/job/<job_number>', methods=['GET'])
def get_job(job_number):
    """
    Returns details for a specific job by job number.
    """
    try:
        filter_formula = f"{{Job Number}}='{job_number}'"
        
        params = {
            'filterByFormula': filter_formula,
            'maxRecords': 1
        }
        
        response = requests.get(PROJECTS_URL, headers=get_headers(), params=params)
        response.raise_for_status()
        data = response.json()
        
        records = data.get('records', [])
        if not records:
            return jsonify({'error': 'Job not found'}), 404
        
        record = records[0]
        fields = record.get('fields', {})
        
        return jsonify({
            'recordId': record.get('id'),
            'jobNumber': fields.get('Job Number'),
            'projectName': fields.get('Project Name'),
            'client': fields.get('Client'),
            'status': fields.get('Status'),
            'stage': fields.get('Stage'),
            'description': fields.get('Description'),
            'updateSummary': fields.get('Update Summary'),
            'projectOwner': fields.get('Project Owner')
        })
    
    except requests.exceptions.RequestException as e:
        print(f"Airtable API error: {e}")
        return jsonify({'error': 'Failed to fetch job'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
