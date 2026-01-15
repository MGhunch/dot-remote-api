"""
Dot Remote API
Flask server for Airtable integration.
Ask Dot brain lives in ask_dot.py
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os
import json
from datetime import datetime
import re

# Import Ask Dot brain
import ask_dot

app = Flask(__name__)
CORS(app)

AIRTABLE_API_KEY = os.environ.get('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID', 'app8CI7NAZqhQ4G1Y')

HEADERS = {
    'Authorization': f'Bearer {AIRTABLE_API_KEY}',
    'Content-Type': 'application/json'
}

def get_airtable_url(table):
    return f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}'


# ===== DATE PARSING HELPERS =====
def parse_friendly_date(friendly_str):
    """Parse friendly date formats into ISO format"""
    if not friendly_str or friendly_str.upper() == 'TBC':
        return None
    
    match = re.search(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', friendly_str, re.IGNORECASE)
    if match:
        day = int(match.group(1))
        month_str = match.group(2).capitalize()
        months = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                  'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}
        month = months.get(month_str)
        if month:
            year = datetime.now().year
            try:
                date = datetime(year, month, day)
                if (datetime.now() - date).days > 180:
                    date = datetime(year + 1, month, day)
                return date.strftime('%Y-%m-%d')
            except ValueError:
                return None
    
    try:
        date = datetime.strptime(friendly_str, '%d %B %Y')
        return date.strftime('%Y-%m-%d')
    except ValueError:
        pass
    
    return None

def parse_status_changed(status_str):
    """Parse Status Changed field into ISO date"""
    if not status_str:
        return None
    
    if 'T' in status_str:
        try:
            date_part = status_str.split('T')[0]
            return date_part
        except:
            pass
    
    match = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', status_str)
    if match:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        try:
            return datetime(year, month, day).strftime('%Y-%m-%d')
        except ValueError:
            return None
    
    return None

def extract_client_code(job_number):
    """Extract client code from job number like 'SKY 017' -> 'SKY'"""
    if not job_number:
        return None
    parts = job_number.split(' ')
    return parts[0] if parts else None

def transform_project(record):
    """Transform Airtable record to frontend format"""
    fields = record.get('fields', {})
    job_number = fields.get('Job Number', '')
    
    update_summary = fields.get('Update Summary', '') or fields.get('Update', '')
    latest_update = update_summary
    if '|' in update_summary:
        parts = update_summary.split('|')
        latest_update = parts[-1].strip() if parts else update_summary
    
    update_due_friendly = fields.get('Update due friendly', '')
    update_due = parse_friendly_date(update_due_friendly)
    
    live_date_raw = fields.get('Live Date', '')
    live_date = parse_friendly_date(live_date_raw) if live_date_raw else None
    
    last_update_made = fields.get('Last update made', '')
    last_updated = parse_status_changed(last_update_made)
    
    with_client = bool(fields.get('With Client?', False))
    
    # Get update history - could be array or string
    update_history_raw = fields.get('Update history', [])
    if isinstance(update_history_raw, str):
        # If it's a string, split by newlines or some delimiter
        update_history = [u.strip() for u in update_history_raw.split('\n') if u.strip()]
    elif isinstance(update_history_raw, list):
        update_history = update_history_raw
    else:
        update_history = []
    
    return {
        'jobNumber': job_number,
        'jobName': fields.get('Project Name', ''),
        'clientCode': extract_client_code(job_number),
        'client': fields.get('Client', ''),
        'description': fields.get('Description', ''),
        'projectOwner': fields.get('Project Owner', ''),
        'update': latest_update,
        'updateHistory': update_history,
        'updateDue': update_due,
        'liveDate': live_date,
        'lastUpdated': last_updated,
        'stage': fields.get('Stage', 'Triage'),
        'status': fields.get('Status', 'Incoming'),
        'withClient': with_client,
        'channelUrl': fields.get('Channel Url', ''),
        'teamsChannelId': fields.get('Teams Channel ID', '')
    }


# ===== HEALTH CHECK =====
@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'dot-remote-api'})


# ===== CLIENTS =====
@app.route('/clients')
def get_clients():
    try:
        url = get_airtable_url('Clients')
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        clients = []
        for record in records:
            fields = record.get('fields', {})
            clients.append({
                'code': fields.get('Client code', ''),
                'name': fields.get('Clients', ''),
                'teamsId': fields.get('Teams ID', ''),
                'sharepointId': fields.get('Sharepoint ID', '')
            })
        
        clients.sort(key=lambda x: x['name'])
        return jsonify(clients)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===== PEOPLE =====
@app.route('/people/<client_code>')
def get_people_for_client(client_code):
    """Get people/contacts for a specific client"""
    try:
        url = get_airtable_url('People')
        
        # Handle One NZ divisions - search for ONE, ONB, or ONS
        if client_code in ['ONE', 'ONB', 'ONS']:
            filter_formula = "AND({Active} = TRUE(), OR({Client Link} = 'ONE', {Client Link} = 'ONB', {Client Link} = 'ONS'))"
        else:
            filter_formula = f"AND({{Active}} = TRUE(), {{Client Link}} = '{client_code}')"
        
        params = {
            'filterByFormula': filter_formula
        }
        
        all_people = []
        offset = None
        
        while True:
            if offset:
                params['offset'] = offset
            
            response = requests.get(url, headers=HEADERS, params=params)
            response.raise_for_status()
            data = response.json()
            
            for record in data.get('records', []):
                fields = record.get('fields', {})
                name = fields.get('Name', fields.get('Full name', ''))
                if name:
                    all_people.append({
                        'name': name,
                        'email': fields.get('Email Address', ''),
                        'clientCode': fields.get('Client Link', '')
                    })
            
            offset = data.get('offset')
            if not offset:
                break
        
        # Sort by name
        all_people.sort(key=lambda x: x['name'])
        return jsonify(all_people)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===== JOBS =====
@app.route('/jobs/all')
def get_all_jobs():
    """Get all active jobs"""
    try:
        url = get_airtable_url('Projects')
        
        active_statuses = ['Incoming', 'In Progress', 'On Hold']
        formula_parts = [f"{{Status}} = '{s}'" for s in active_statuses]
        params = {
            'filterByFormula': f"OR({', '.join(formula_parts)})"
        }
        
        all_records = []
        offset = None
        
        while True:
            if offset:
                params['offset'] = offset
            
            response = requests.get(url, headers=HEADERS, params=params)
            response.raise_for_status()
            data = response.json()
            
            for record in data.get('records', []):
                all_records.append(transform_project(record))
            
            offset = data.get('offset')
            if not offset:
                break
        
        return jsonify(all_records)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/jobs/client/<client_code>')
def get_client_jobs(client_code):
    """Get all jobs for a specific client"""
    try:
        url = get_airtable_url('Projects')
        params = {
            'filterByFormula': f"AND(FIND('{client_code}', {{Job Number}}), {{Status}} != 'Archived')"
        }
        
        all_records = []
        offset = None
        
        while True:
            if offset:
                params['offset'] = offset
            
            response = requests.get(url, headers=HEADERS, params=params)
            response.raise_for_status()
            data = response.json()
            
            for record in data.get('records', []):
                all_records.append(transform_project(record))
            
            offset = data.get('offset')
            if not offset:
                break
        
        return jsonify(all_records)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/job/<job_number>/update', methods=['POST'])
def update_job(job_number):
    """Update a job's fields"""
    try:
        data = request.get_json()
        
        url = get_airtable_url('Projects')
        params = {
            'filterByFormula': f"{{Job Number}} = '{job_number}'",
            'maxRecords': 1
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return jsonify({'error': 'Job not found'}), 404
        
        record_id = records[0].get('id')
        
        field_mapping = {
            'stage': 'Stage',
            'status': 'Status',
            'updateDue': 'Update Due',
            'liveDate': 'Live Date',
            'withClient': 'With Client?',
            'description': 'Description',
            'projectOwner': 'Project Owner'
        }
        
        airtable_fields = {}
        for key, value in data.items():
            if key in field_mapping:
                airtable_key = field_mapping[key]
                if key in ['updateDue', 'liveDate'] and value:
                    airtable_fields[airtable_key] = value
                elif key == 'withClient':
                    airtable_fields[airtable_key] = bool(value)
                else:
                    airtable_fields[airtable_key] = value
        
        if not airtable_fields:
            return jsonify({'error': 'No valid fields to update'}), 400
        
        update_response = requests.patch(
            f"{url}/{record_id}",
            headers=HEADERS,
            json={'fields': airtable_fields}
        )
        update_response.raise_for_status()
        
        return jsonify({'success': True, 'updated': list(airtable_fields.keys())})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===== TRACKER =====
@app.route('/tracker/clients')
def get_tracker_clients():
    """Get clients with tracker/budget info"""
    try:
        url = get_airtable_url('Clients')
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        
        clients = []
        for record in response.json().get('records', []):
            fields = record.get('fields', {})
            
            def parse_currency(val):
                if isinstance(val, (int, float)):
                    return val
                if isinstance(val, str):
                    return int(val.replace('$', '').replace(',', '') or 0)
                return 0
            
            monthly = parse_currency(fields.get('Monthly Committed', 0))
            if monthly > 0:
                rollover = fields.get('Rollover Credit', 0)
                if isinstance(rollover, list):
                    rollover = rollover[0] if rollover else 0
                rollover = parse_currency(rollover)
                
                clients.append({
                    'code': fields.get('Client code', ''),
                    'name': fields.get('Clients', ''),
                    'committed': monthly,
                    'rollover': rollover,
                    'rolloverUseIn': fields.get('Rollover use', ''),
                    'yearEnd': fields.get('Year end', ''),
                    'currentQuarter': fields.get('Current Quarter', '')
                })
        
        clients.sort(key=lambda x: x['name'])
        return jsonify(clients)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tracker/data')
def get_tracker_data():
    """Get tracker spend data for a client"""
    client_code = request.args.get('client')
    if not client_code:
        return jsonify({'error': 'Client code required'}), 400
    
    try:
        projects_url = get_airtable_url('Projects')
        projects_response = requests.get(projects_url, headers=HEADERS, params={
            'filterByFormula': f"FIND('{client_code}', {{Job Number}})"
        })
        projects_response.raise_for_status()
        
        projects_map = {}
        for record in projects_response.json().get('records', []):
            fields = record.get('fields', {})
            job_number = fields.get('Job Number', '')
            if job_number:
                projects_map[job_number] = {
                    'projectName': fields.get('Project Name', ''),
                    'owner': fields.get('Project Owner', '')
                }
        
        tracker_url = get_airtable_url('Tracker')
        
        all_records = []
        offset = None
        
        while True:
            params = {
                'filterByFormula': f"FIND('{client_code}', {{Job Number}})"
            }
            if offset:
                params['offset'] = offset
            
            response = requests.get(tracker_url, headers=HEADERS, params=params)
            response.raise_for_status()
            data = response.json()
            
            for record in data.get('records', []):
                fields = record.get('fields', {})
                job_number = fields.get('Job Number', '')
                
                # Handle linked field (returns as list)
                if isinstance(job_number, list):
                    job_number = job_number[0] if job_number else ''
                
                spend = fields.get('Spend', 0)
                if isinstance(spend, str):
                    spend = float(spend.replace('$', '').replace(',', '') or 0)
                
                if not job_number or spend == 0:
                    continue
                
                project = projects_map.get(job_number, {})
                
                all_records.append({
                    'id': record.get('id'),
                    'client': client_code,
                    'jobNumber': job_number,
                    'projectName': project.get('projectName', ''),
                    'owner': project.get('owner', ''),
                    'description': fields.get('Tracker notes', ''),
                    'spend': spend,
                    'month': fields.get('Month', ''),
                    'spendType': fields.get('Spend type', 'Project budget'),
                    'ballpark': bool(fields.get('Ballpark', False)),
                })
            
            offset = data.get('offset')
            if not offset:
                break
        
        return jsonify(all_records)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tracker/update', methods=['POST'])
def update_tracker():
    """Update a tracker record"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        record_id = data.get('id')
        if not record_id:
            return jsonify({'error': 'Record ID required'}), 400
        
        field_mapping = {
            'description': 'Tracker notes',
            'spend': 'Spend',
            'month': 'Month',
            'spendType': 'Spend type',
            'ballpark': 'Ballpark'
        }
        
        airtable_fields = {}
        for key, value in data.items():
            if key in field_mapping:
                airtable_key = field_mapping[key]
                airtable_fields[airtable_key] = value
        
        if not airtable_fields:
            return jsonify({'error': 'No valid fields to update'}), 400
        
        url = get_airtable_url('Tracker')
        response = requests.patch(
            f"{url}/{record_id}",
            headers=HEADERS,
            json={'fields': airtable_fields}
        )
        response.raise_for_status()
        
        return jsonify({'success': True})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===== ASK DOT (Claude) =====
@app.route('/claude/parse', methods=['POST'])
def claude_parse():
    """Process a question through Ask Dot"""
    data = request.get_json()
    question = data.get('question', '')
    clients = data.get('clients', [])
    session_id = data.get('sessionId', 'default')
    
    result = ask_dot.process_question(question, clients, session_id)
    
    if 'error' in result:
        return jsonify(result), 500
    
    return jsonify(result)


@app.route('/claude/clear', methods=['POST'])
def clear_session():
    """Clear conversation history for a session"""
    data = request.get_json()
    session_id = data.get('sessionId', 'default')
    
    ask_dot.clear_conversation(session_id)
    
    return jsonify({'success': True})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
