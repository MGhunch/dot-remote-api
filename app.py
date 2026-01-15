"""
Dot Remote API
Flask server for Airtable integration and Claude processing
Enhanced with Airtable tool access for Ask Dot
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os
import json
import time
from datetime import datetime
import re

app = Flask(__name__)
CORS(app)

AIRTABLE_API_KEY = os.environ.get('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID', 'app8CI7NAZqhQ4G1Y')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')

HEADERS = {
    'Authorization': f'Bearer {AIRTABLE_API_KEY}',
    'Content-Type': 'application/json'
}

def get_airtable_url(table):
    return f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}'


# ===== CONVERSATION MEMORY =====
# Simple in-memory store - sessions expire after 30 mins
conversations = {}
SESSION_TIMEOUT = 30 * 60  # 30 minutes

def get_conversation(session_id):
    """Get or create conversation history for a session"""
    now = time.time()
    
    # Clean up old sessions
    expired = [sid for sid, data in conversations.items() if now - data['last_active'] > SESSION_TIMEOUT]
    for sid in expired:
        del conversations[sid]
    
    if session_id not in conversations:
        conversations[session_id] = {
            'messages': [],
            'context': {},  # Stores last client, job, etc.
            'last_active': now
        }
    else:
        conversations[session_id]['last_active'] = now
    
    return conversations[session_id]

def add_to_conversation(session_id, role, content):
    """Add a message to conversation history"""
    conv = get_conversation(session_id)
    conv['messages'].append({'role': role, 'content': content})
    
    # Keep only last 10 exchanges (20 messages)
    if len(conv['messages']) > 20:
        conv['messages'] = conv['messages'][-20:]

def update_context(session_id, context_update):
    """Update conversation context (last client, job, etc.)"""
    conv = get_conversation(session_id)
    conv['context'].update(context_update)


# ===== DATE PARSING HELPERS =====
def parse_friendly_date(friendly_str):
    """
    Parse friendly date formats like 'Wed 14 Jan', 'Mon 05 Jan', 'TBC' 
    into ISO format (YYYY-MM-DD). Assumes current/next occurrence.
    """
    if not friendly_str or friendly_str.upper() == 'TBC':
        return None
    
    # Try to extract day and month from strings like "Wed 14 Jan" or "Mon 05 Jan"
    match = re.search(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', friendly_str, re.IGNORECASE)
    if match:
        day = int(match.group(1))
        month_str = match.group(2).capitalize()
        months = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                  'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}
        month = months.get(month_str)
        if month:
            # Assume current year, but if date is in past, might be next year
            year = datetime.now().year
            try:
                date = datetime(year, month, day)
                # If date is more than 6 months in past, assume next year
                if (datetime.now() - date).days > 180:
                    date = datetime(year + 1, month, day)
                return date.strftime('%Y-%m-%d')
            except ValueError:
                return None
    
    # Try parsing full date formats like "22 January 2026"
    try:
        date = datetime.strptime(friendly_str, '%d %B %Y')
        return date.strftime('%Y-%m-%d')
    except ValueError:
        pass
    
    # Try "Jan 26" format (month + 2-digit year)
    match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})$', friendly_str, re.IGNORECASE)
    if match:
        month_str = match.group(1).capitalize()
        year = 2000 + int(match.group(2))
        months = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                  'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}
        month = months.get(month_str)
        if month:
            # Use last day of month as approximate
            return f'{year}-{month:02d}-28'
    
    return None

def parse_status_changed(status_str):
    """
    Parse 'Status Changed' field into ISO date.
    Airtable API returns ISO format: '2025-12-26T11:03:00.000Z'
    Display shows: '26/12/2025 11:03am'
    """
    if not status_str:
        return None
    
    # Try ISO format first (what Airtable API actually returns)
    if 'T' in status_str:
        try:
            # Handle '2025-12-26T11:03:00.000Z' format
            date_part = status_str.split('T')[0]
            return date_part
        except:
            pass
    
    # Fallback: Try DD/MM/YYYY format (in case of CSV import)
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
    
    # Parse Update Summary to get latest update text
    update_summary = fields.get('Update Summary', '') or fields.get('Update', '')
    # Extract just the latest update (often formatted as "DD-Mon | message")
    latest_update = update_summary
    if '|' in update_summary:
        # Get the message part after the last pipe
        parts = update_summary.split('|')
        latest_update = parts[-1].strip() if parts else update_summary
    
    # Parse dates
    update_due_friendly = fields.get('Update due friendly', '')
    update_due = parse_friendly_date(update_due_friendly)
    
    live_date_raw = fields.get('Live Date', '')
    live_date = parse_friendly_date(live_date_raw) if live_date_raw else None
    
    # Use Last update made (rollup from Updates table) as lastUpdated
    last_update_made = fields.get('Last update made', '')
    last_updated = parse_status_changed(last_update_made)
    
    # With Client - Airtable checkbox returns True/False (or missing if unchecked)
    with_client = bool(fields.get('With Client?', False))
    
    return {
        'jobNumber': job_number,
        'jobName': fields.get('Project Name', ''),
        'clientCode': extract_client_code(job_number),
        'client': fields.get('Client', ''),
        'description': fields.get('Tracker notes', ''),
        'projectOwner': fields.get('Project Owner', ''),
        'update': latest_update,
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


# ===== PIN VALIDATION =====
@app.route('/pin/validate', methods=['POST'])
def validate_pin():
    """
    Validate a PIN and return user permissions.
    Checks People table first, then falls back to hardcoded Hunch PINs.
    Returns: { valid, mode, clientCode, trackerAccess, name, fullName }
    """
    data = request.get_json()
    pin = data.get('pin', '')
    
    if not pin or len(pin) != 4:
        return jsonify({'valid': False, 'error': 'Invalid PIN format'}), 400
    
    # Hardcoded Hunch team PINs (fallback)
    HUNCH_PINS = {
        '9871': {'name': 'Michael', 'fullName': 'Michael Goldthorpe', 'mode': 'hunch', 'trackerAccess': True},
        '1919': {'name': 'Team', 'fullName': 'Hunch Team', 'mode': 'hunch', 'trackerAccess': True}
    }
    
    # Check hardcoded PINs first (for Hunch team)
    if pin in HUNCH_PINS:
        user = HUNCH_PINS[pin]
        return jsonify({
            'valid': True,
            'mode': user['mode'],
            'clientCode': None,
            'trackerAccess': user['trackerAccess'],
            'name': user['name'],
            'fullName': user['fullName']
        })
    
    # Check People table for client PINs
    try:
        url = get_airtable_url('People')
        params = {
            'filterByFormula': f"{{Pin}} = '{pin}'",
            'maxRecords': 1
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if records:
            fields = records[0].get('fields', {})
            client_link = fields.get('Client Link', '')
            
            # Handle if Client Link is an array (linked record)
            if isinstance(client_link, list):
                client_link = client_link[0] if client_link else ''
            
            first_name = fields.get('First Name', '')
            full_name = fields.get('Name', fields.get('Full name', first_name))
            
            return jsonify({
                'valid': True,
                'mode': 'client',
                'clientCode': client_link,
                'trackerAccess': False,  # Clients don't get tracker access by default
                'name': first_name or full_name.split()[0] if full_name else 'Guest',
                'fullName': full_name
            })
    
    except Exception as e:
        print(f'PIN lookup error: {e}')
        # Fall through to invalid
    
    return jsonify({'valid': False, 'error': 'PIN not recognised'})


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
        
        # Sort by name
        clients.sort(key=lambda x: x['name'])
        return jsonify(clients)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/clients/detail/<client_code>')
def get_client_detail(client_code):
    """
    Get detailed client info including commercial setup.
    Used by Dot when answering questions about a specific client.
    """
    try:
        url = get_airtable_url('Clients')
        params = {
            'filterByFormula': f"{{Client code}} = '{client_code}'",
            'maxRecords': 1
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return jsonify({'error': f'Client {client_code} not found'}), 404
        
        fields = records[0].get('fields', {})
        
        # Parse currency fields
        def parse_currency(val):
            if isinstance(val, (int, float)):
                return val
            if isinstance(val, str):
                return int(val.replace('$', '').replace(',', '') or 0)
            return 0
        
        # Parse rollover (might be array from lookup)
        rollover = fields.get('Rollover Credit', 0)
        if isinstance(rollover, list):
            rollover = rollover[0] if rollover else 0
        rollover = parse_currency(rollover)
        
        return jsonify({
            'code': client_code,
            'name': fields.get('Clients', ''),
            'yearEnd': fields.get('Year end', ''),
            'currentQuarter': fields.get('Current Quarter', ''),
            'monthlyCommitted': parse_currency(fields.get('Monthly Committed', 0)),
            'quarterlyCommitted': parse_currency(fields.get('Quarterly Committed', 0)),
            'thisMonth': parse_currency(fields.get('This month', 0)),
            'thisQuarter': parse_currency(fields.get('This Quarter', 0)),
            'undersOvers': parse_currency(fields.get('Unders/Overs', 0)),
            'rolloverCredit': rollover,
            'teamsId': fields.get('Teams ID', ''),
            'sharepointUrl': fields.get('Sharepoint ID', '')
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===== PEOPLE =====
@app.route('/people')
def get_people():
    """
    Search People table.
    Query params:
    - client: filter by client code (optional)
    - search: search term for name/email (optional)
    - active: filter to active only (default true)
    """
    client_code = request.args.get('client')
    search_term = request.args.get('search', '').lower()
    active_only = request.args.get('active', 'true').lower() == 'true'
    
    try:
        url = get_airtable_url('People')
        
        # Build filter formula
        filters = []
        if client_code:
            filters.append(f"{{Client Link}} = '{client_code}'")
        if active_only:
            filters.append("{Active} = TRUE()")
        
        params = {}
        if filters:
            params['filterByFormula'] = f"AND({', '.join(filters)})" if len(filters) > 1 else filters[0]
        
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
                
                # Skip empty records
                if not name:
                    continue
                
                # Apply search filter (client-side for flexibility)
                if search_term:
                    searchable = f"{name} {fields.get('Email Address', '')}".lower()
                    if search_term not in searchable:
                        continue
                
                all_people.append({
                    'name': name,
                    'firstName': fields.get('First Name', ''),
                    'lastName': fields.get('Last Name', ''),
                    'email': fields.get('Email Address', ''),
                    'phone': fields.get('Phone Number', ''),
                    'clientCode': fields.get('Client Link', ''),
                    'active': bool(fields.get('Active', False)),
                    'birthday': fields.get('Birthday', ''),
                    'notes': fields.get('Notes', '')
                })
            
            offset = data.get('offset')
            if not offset:
                break
        
        # Sort by name
        all_people.sort(key=lambda x: x['name'])
        
        return jsonify({
            'count': len(all_people),
            'people': all_people
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===== JOBS =====
@app.route('/jobs')
def get_jobs():
    """Get jobs for a specific client (matches existing format for remote.html)"""
    client_code = request.args.get('client')
    if not client_code:
        return jsonify({'error': 'client parameter required'}), 400
    
    try:
        url = get_airtable_url('Projects')
        # Filter by client code prefix in Job Number
        params = {
            'filterByFormula': f"AND(SEARCH('{client_code} ', {{Job Number}}) = 1, NOT(OR({{Status}} = 'Completed', {{Status}} = 'Archived')))"
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        
        # Return in EXISTING format for backwards compatibility with remote.html
        jobs = []
        for record in records:
            fields = record.get('fields', {})
            job_number = fields.get('Job Number', '')
            project_name = fields.get('Project Name', '')
            jobs.append({
                'id': job_number,
                'name': f"{job_number} – {project_name}" if project_name else job_number,
                'recordId': record.get('id', ''),
                'stage': fields.get('Stage', 'Triage'),
                'status': fields.get('Status', 'Incoming')
            })
        
        return jsonify(jobs)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/jobs/all')
def get_all_jobs():
    """Get all active jobs for What's What / WIP board"""
    try:
        url = get_airtable_url('Projects')
        params = {
            'filterByFormula': "NOT({Status} = 'Archived')"
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
        
        # Sort by update due date
        all_records.sort(key=lambda x: x['updateDue'] or '9999-99-99')
        return jsonify(all_records)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/job/<job_number>')
def get_job(job_number):
    """Get a single job by job number"""
    try:
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
        
        return jsonify(transform_project(records[0]))
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/job/<job_number>/update', methods=['POST'])
def update_job(job_number):
    """Update a job's fields"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # First, find the record ID
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
        
        record_id = records[0]['id']
        
        # Map frontend field names to Airtable field names
        field_mapping = {
            'stage': 'Stage',
            'status': 'Status',
            'updateDue': 'Update due friendly',
            'liveDate': 'Live Date',
            'withClient': 'With Client?'
        }
        
        airtable_fields = {}
        for key, value in data.items():
            if key in field_mapping:
                airtable_fields[field_mapping[key]] = value
        
        if not airtable_fields:
            return jsonify({'error': 'No valid fields to update'}), 400
        
        # Update the record
        update_url = f"{url}/{record_id}"
        response = requests.patch(
            update_url,
            headers=HEADERS,
            json={'fields': airtable_fields}
        )
        response.raise_for_status()
        
        return jsonify({'success': True, 'updated': list(airtable_fields.keys())})
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ===== TRACKER =====
@app.route('/tracker/clients')
def get_tracker_clients():
    """Get clients with committed retainer budgets for tracker"""
    try:
        url = get_airtable_url('Clients')
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        clients = []
        
        for record in records:
            fields = record.get('fields', {})
            
            # Monthly Committed - could be number or currency string
            monthly_committed = fields.get('Monthly Committed', 0)
            # Parse currency string like "$25000" to number
            if isinstance(monthly_committed, str):
                monthly_committed = int(monthly_committed.replace('$', '').replace(',', '') or 0)
            
            # Rollover Credit - could be number or array (from lookup)
            rollover_credit = fields.get('Rollover Credit', 0)
            if isinstance(rollover_credit, list):
                rollover_credit = rollover_credit[0] if rollover_credit else 0
            if isinstance(rollover_credit, str):
                rollover_credit = int(rollover_credit.replace('$', '').replace(',', '') or 0)
            
            # Rollover Use In - which quarter this rollover applies to (e.g., "JAN-MAR")
            rollover_use_in = fields.get('Rollover use', '')
            if isinstance(rollover_use_in, list):
                rollover_use_in = rollover_use_in[0] if rollover_use_in else ''
            
            clients.append({
                'code': fields.get('Client code', ''),
                'name': fields.get('Clients', ''),
                'committed': monthly_committed,
                'yearEnd': fields.get('Year end', ''),
                'currentQuarter': fields.get('Current Quarter', ''),
                'rollover': rollover_credit,
                'rolloverUseIn': rollover_use_in
            })
        
        # Filter to only clients with committed spend > 0
        clients = [c for c in clients if c['committed'] > 0]
        clients.sort(key=lambda x: x['name'])
        return jsonify(clients)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tracker/data')
def get_tracker_data():
    """
    Get tracker data for a specific client.
    Joins Tracker table (spend data) with Projects table (job details).
    Query params:
    - client: client code (required)
    """
    client_code = request.args.get('client')
    if not client_code:
        return jsonify({'error': 'client parameter required'}), 400
    
    try:
        # Step 1: Get all Projects for this client (need record IDs and job details)
        projects_url = get_airtable_url('Projects')
        projects_filter = f"SEARCH('{client_code} ', {{Job Number}}) = 1"
        
        # Map by RECORD ID (for joining) and by Job Number (for output)
        projects_by_record_id = {}  # airtable_record_id -> {jobNumber, projectName, owner}
        offset = None
        
        while True:
            params = {'filterByFormula': projects_filter}
            if offset:
                params['offset'] = offset
            
            response = requests.get(projects_url, headers=HEADERS, params=params)
            response.raise_for_status()
            data = response.json()
            
            for record in data.get('records', []):
                record_id = record.get('id')
                fields = record.get('fields', {})
                job_number = fields.get('Job Number', '')
                if record_id and job_number:
                    projects_by_record_id[record_id] = {
                        'jobNumber': job_number,
                        'projectName': fields.get('Project Name', ''),
                        'owner': fields.get('Project Owner', ''),
                        'client': fields.get('Client', '')
                    }
            
            offset = data.get('offset')
            if not offset:
                break
        
        # Step 2: Get all Tracker records for this client
        tracker_url = get_airtable_url('Tracker')
        tracker_filter = f"{{Client Code}} = '{client_code}'"
        
        all_records = []
        offset = None
        
        while True:
            params = {'filterByFormula': tracker_filter}
            if offset:
                params['offset'] = offset
            
            response = requests.get(tracker_url, headers=HEADERS, params=params)
            response.raise_for_status()
            data = response.json()
            
            for record in data.get('records', []):
                fields = record.get('fields', {})
                
                # Parse spend - handle currency strings or numbers
                spend = fields.get('Spend', 0)
                if isinstance(spend, str):
                    spend = int(spend.replace('$', '').replace(',', '') or 0)
                
                # Get the linked Project record ID(s) from Job Number field
                job_link = fields.get('Job Number', [])
                if isinstance(job_link, list) and len(job_link) > 0:
                    project_record_id = job_link[0]  # First linked record
                else:
                    project_record_id = job_link if isinstance(job_link, str) else None
                
                # Look up project details by record ID
                project = projects_by_record_id.get(project_record_id, {})
                job_number = project.get('jobNumber', '')
                
                # Skip if we can't identify the job
                if not job_number:
                    continue
                
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
                    'onUs': bool(fields.get('On us', False)),
                    'status': fields.get('Status', 'Active')
                })
            
            offset = data.get('offset')
            if not offset:
                break
        
        return jsonify(all_records)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tracker/update', methods=['POST'])
def update_tracker_record():
    """
    Update a tracker record.
    Body: {
        "id": "recXXXXX",  # Airtable record ID
        "description": "...",
        "spend": 5000,
        "month": "January",
        "spendType": "Project budget",
        "ballpark": true/false
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        record_id = data.get('id')
        if not record_id:
            return jsonify({'error': 'Record ID required'}), 400
        
        # Map frontend fields to Airtable Tracker fields
        # Note: Project Name and Owner live in Projects table, not Tracker
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
                # Handle ballpark checkbox - Airtable wants boolean
                if key == 'ballpark':
                    airtable_fields[airtable_key] = bool(value)
                else:
                    airtable_fields[airtable_key] = value
        
        if not airtable_fields:
            return jsonify({'error': 'No valid fields to update'}), 400
        
        # Update the record
        url = f"{get_airtable_url('Tracker')}/{record_id}"
        response = requests.patch(
            url,
            headers=HEADERS,
            json={'fields': airtable_fields}
        )
        response.raise_for_status()
        
        return jsonify({
            'success': True,
            'recordId': record_id,
            'updated': list(airtable_fields.keys())
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tracker/summary')
def get_tracker_summary():
    """
    Get a spend summary for a client and period.
    Uses pre-calculated quarter columns from Clients table.
    
    Query params:
    - client: client code (required)
    - period: "this_month", "last_month", "this_quarter", "last_quarter", or quarter name like "JAN-MAR"
    """
    client_code = request.args.get('client')
    period = request.args.get('period', 'this_quarter')
    
    if not client_code:
        return jsonify({'error': 'client parameter required'}), 400
    
    try:
        # Get client info from Clients table (has pre-calculated quarter spend)
        clients_url = get_airtable_url('Clients')
        clients_response = requests.get(clients_url, headers=HEADERS)
        clients_response.raise_for_status()
        
        client_info = None
        for record in clients_response.json().get('records', []):
            fields = record.get('fields', {})
            if fields.get('Client code', '') == client_code:
                # Parse monthly committed
                monthly_committed = fields.get('Monthly Committed', 0)
                if isinstance(monthly_committed, str):
                    monthly_committed = float(monthly_committed.replace('$', '').replace(',', '') or 0)
                
                # Parse rollover credit (might be a list from lookup)
                rollover = fields.get('Rollover Credit', 0)
                if isinstance(rollover, list):
                    rollover = rollover[0] if rollover else 0
                if isinstance(rollover, str):
                    rollover = float(rollover.replace('$', '').replace(',', '') or 0)
                
                # Get rollover use quarter
                rollover_use = fields.get('Rollover use', '')
                
                # Get pre-calculated quarter spends
                def parse_spend(val):
                    if isinstance(val, (int, float)):
                        return float(val)
                    if isinstance(val, str):
                        return float(val.replace('$', '').replace(',', '') or 0)
                    return 0
                
                client_info = {
                    'name': fields.get('Clients', ''),
                    'code': client_code,
                    'monthlyBudget': monthly_committed,
                    'quarterlyBudget': monthly_committed * 3,
                    'currentQuarter': fields.get('Current Quarter', ''),
                    'yearEnd': fields.get('Year end', ''),
                    'rollover': rollover,
                    'rolloverUse': rollover_use,
                    # Pre-calculated quarter spends from Airtable
                    'JAN-MAR': parse_spend(fields.get('JAN-MAR', 0)),
                    'APR-JUN': parse_spend(fields.get('APR-JUN', 0)),
                    'JUL-SEP': parse_spend(fields.get('JUL-SEP', 0)),
                    'OCT-DEC': parse_spend(fields.get('OCT-DEC', 0)),
                    # This month/quarter from Airtable
                    'thisMonth': parse_spend(fields.get('This month', 0)),
                    'thisQuarter': parse_spend(fields.get('This Quarter', 0)),
                }
                break
        
        if not client_info:
            return jsonify({'error': f'Client {client_code} not found'}), 404
        
        # Map period to quarter column
        now = datetime.now()
        current_month_num = now.month
        
        # Determine which calendar quarter we're in
        calendar_quarters = {
            1: 'JAN-MAR', 2: 'JAN-MAR', 3: 'JAN-MAR',
            4: 'APR-JUN', 5: 'APR-JUN', 6: 'APR-JUN',
            7: 'JUL-SEP', 8: 'JUL-SEP', 9: 'JUL-SEP',
            10: 'OCT-DEC', 11: 'OCT-DEC', 12: 'OCT-DEC'
        }
        current_cal_quarter = calendar_quarters[current_month_num]
        
        # Previous calendar quarter
        prev_quarters = {
            'JAN-MAR': 'OCT-DEC',
            'APR-JUN': 'JAN-MAR',
            'JUL-SEP': 'APR-JUN',
            'OCT-DEC': 'JUL-SEP'
        }
        last_cal_quarter = prev_quarters[current_cal_quarter]
        
        # Figure out what to return based on period
        if period == 'this_quarter':
            quarter_key = current_cal_quarter
            period_label = client_info['currentQuarter']  # Use client's Q label (e.g., "Q2" for Tower)
        elif period == 'last_quarter':
            quarter_key = last_cal_quarter
            # Calculate client's previous quarter label
            current_q_num = int(client_info['currentQuarter'].replace('Q', '') or 1)
            last_q_num = current_q_num - 1 if current_q_num > 1 else 4
            period_label = f'Q{last_q_num}'
        elif period in ['JAN-MAR', 'APR-JUN', 'JUL-SEP', 'OCT-DEC']:
            quarter_key = period
            period_label = period
        elif period == 'this_month':
            # For monthly, use the This month column
            spent = client_info['thisMonth']
            budget = client_info['monthlyBudget']
            remaining = budget - spent
            percent_used = round((spent / budget * 100) if budget > 0 else 0)
            
            return jsonify({
                'client': client_info['name'],
                'clientCode': client_code,
                'period': now.strftime('%B'),
                'budget': budget,
                'spent': spent,
                'remaining': remaining,
                'percentUsed': percent_used,
                'status': 'over' if percent_used > 100 else ('high' if percent_used > 80 else 'on_track')
            })
        else:
            # Default to this quarter
            quarter_key = current_cal_quarter
            period_label = client_info['currentQuarter']
        
        # Get spent from pre-calculated column
        spent = client_info.get(quarter_key, 0)
        
        # Calculate budget (quarterly + rollover if applicable)
        budget = client_info['quarterlyBudget']
        
        # Add rollover if it applies to this quarter
        if client_info['rolloverUse'] == quarter_key and client_info['rollover'] > 0:
            budget += client_info['rollover']
        
        remaining = budget - spent
        percent_used = round((spent / budget * 100) if budget > 0 else 0)
        
        # Determine status
        if percent_used > 100:
            status = 'over'
        elif percent_used > 80:
            status = 'high'
        else:
            status = 'on_track'
        
        return jsonify({
            'client': client_info['name'],
            'clientCode': client_code,
            'period': period_label,
            'budget': budget,
            'spent': spent,
            'remaining': remaining,
            'percentUsed': percent_used,
            'status': status,
            'rolloverApplied': client_info['rolloverUse'] == quarter_key and client_info['rollover'] > 0,
            'rolloverAmount': client_info['rollover'] if client_info['rolloverUse'] == quarter_key else 0
        })
    
    except Exception as e:
        app.logger.error(f"Tracker summary error: {e}")
        return jsonify({'error': str(e)}), 500


# ===== AIRTABLE TOOLS FOR CLAUDE =====
# These functions are called by Claude during tool use

def tool_search_people(client_code=None, search_term=None, active_only=True):
    """
    Search People table. Returns formatted results for Claude.
    """
    try:
        url = get_airtable_url('People')
        
        filters = []
        if client_code:
            filters.append(f"{{Client Link}} = '{client_code}'")
        if active_only:
            filters.append("{Active} = TRUE()")
        
        params = {}
        if filters:
            params['filterByFormula'] = f"AND({', '.join(filters)})" if len(filters) > 1 else filters[0]
        
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
                if not name:
                    continue
                
                if search_term:
                    searchable = f"{name} {fields.get('Email Address', '')}".lower()
                    if search_term.lower() not in searchable:
                        continue
                
                all_people.append({
                    'name': name,
                    'email': fields.get('Email Address', ''),
                    'phone': fields.get('Phone Number', ''),
                    'clientCode': fields.get('Client Link', '')
                })
            
            offset = data.get('offset')
            if not offset:
                break
        
        return {'count': len(all_people), 'people': all_people}
    
    except Exception as e:
        return {'error': str(e)}


def tool_get_client_detail(client_code):
    """
    Get detailed client info. Returns formatted results for Claude.
    """
    try:
        url = get_airtable_url('Clients')
        params = {
            'filterByFormula': f"{{Client code}} = '{client_code}'",
            'maxRecords': 1
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return {'error': f'Client {client_code} not found'}
        
        fields = records[0].get('fields', {})
        
        def parse_currency(val):
            if isinstance(val, (int, float)):
                return val
            if isinstance(val, str):
                return int(val.replace('$', '').replace(',', '') or 0)
            return 0
        
        rollover = fields.get('Rollover Credit', 0)
        if isinstance(rollover, list):
            rollover = rollover[0] if rollover else 0
        rollover = parse_currency(rollover)
        
        return {
            'code': client_code,
            'name': fields.get('Clients', ''),
            'yearEnd': fields.get('Year end', ''),
            'currentQuarter': fields.get('Current Quarter', ''),
            'monthlyCommitted': parse_currency(fields.get('Monthly Committed', 0)),
            'quarterlyCommitted': parse_currency(fields.get('Quarterly Committed', 0)),
            'thisMonth': parse_currency(fields.get('This month', 0)),
            'thisQuarter': parse_currency(fields.get('This Quarter', 0)),
            'rolloverCredit': rollover
        }
    
    except Exception as e:
        return {'error': str(e)}


def tool_get_spend_summary(client_code, period='this_quarter'):
    """
    Get spend summary for a client using pre-calculated quarter columns.
    Returns formatted results for Claude.
    """
    try:
        clients_url = get_airtable_url('Clients')
        clients_response = requests.get(clients_url, headers=HEADERS)
        clients_response.raise_for_status()
        
        client_info = None
        for record in clients_response.json().get('records', []):
            fields = record.get('fields', {})
            if fields.get('Client code', '') == client_code:
                def parse_currency(val):
                    if isinstance(val, (int, float)):
                        return float(val)
                    if isinstance(val, str):
                        return float(val.replace('$', '').replace(',', '') or 0)
                    if isinstance(val, list):
                        return float(val[0]) if val else 0
                    return 0
                
                monthly = parse_currency(fields.get('Monthly Committed', 0))
                rollover = parse_currency(fields.get('Rollover Credit', 0))
                rollover_use = fields.get('Rollover use', '')
                
                client_info = {
                    'name': fields.get('Clients', ''),
                    'code': client_code,
                    'monthlyBudget': monthly,
                    'quarterlyBudget': monthly * 3,
                    'currentQuarter': fields.get('Current Quarter', ''),
                    'rollover': rollover,
                    'rolloverUse': rollover_use,
                    # Pre-calculated quarter spends
                    'JAN-MAR': parse_currency(fields.get('JAN-MAR', 0)),
                    'APR-JUN': parse_currency(fields.get('APR-JUN', 0)),
                    'JUL-SEP': parse_currency(fields.get('JUL-SEP', 0)),
                    'OCT-DEC': parse_currency(fields.get('OCT-DEC', 0)),
                    'thisMonth': parse_currency(fields.get('This month', 0)),
                }
                break
        
        if not client_info:
            return {'error': f'Client {client_code} not found'}
        
        # Map period to quarter column
        now = datetime.now()
        current_month_num = now.month
        
        calendar_quarters = {
            1: 'JAN-MAR', 2: 'JAN-MAR', 3: 'JAN-MAR',
            4: 'APR-JUN', 5: 'APR-JUN', 6: 'APR-JUN',
            7: 'JUL-SEP', 8: 'JUL-SEP', 9: 'JUL-SEP',
            10: 'OCT-DEC', 11: 'OCT-DEC', 12: 'OCT-DEC'
        }
        current_cal_quarter = calendar_quarters[current_month_num]
        
        prev_quarters = {
            'JAN-MAR': 'OCT-DEC',
            'APR-JUN': 'JAN-MAR',
            'JUL-SEP': 'APR-JUN',
            'OCT-DEC': 'JUL-SEP'
        }
        last_cal_quarter = prev_quarters[current_cal_quarter]
        
        # Determine quarter key and label based on period
        if period == 'this_quarter':
            quarter_key = current_cal_quarter
            period_label = client_info['currentQuarter']
        elif period == 'last_quarter':
            quarter_key = last_cal_quarter
            current_q_num = int(client_info['currentQuarter'].replace('Q', '') or 1)
            last_q_num = current_q_num - 1 if current_q_num > 1 else 4
            period_label = f'Q{last_q_num}'
        elif period in ['JAN-MAR', 'APR-JUN', 'JUL-SEP', 'OCT-DEC']:
            quarter_key = period
            period_label = period
        elif period == 'this_month':
            return {
                'client': client_info['name'],
                'clientCode': client_code,
                'period': now.strftime('%B'),
                'budget': client_info['monthlyBudget'],
                'spent': client_info['thisMonth'],
                'remaining': client_info['monthlyBudget'] - client_info['thisMonth'],
                'percentUsed': round((client_info['thisMonth'] / client_info['monthlyBudget'] * 100) if client_info['monthlyBudget'] > 0 else 0)
            }
        else:
            quarter_key = current_cal_quarter
            period_label = client_info['currentQuarter']
        
        spent = client_info.get(quarter_key, 0)
        budget = client_info['quarterlyBudget']
        
        # Add rollover if applicable
        if client_info['rolloverUse'] == quarter_key and client_info['rollover'] > 0:
            budget += client_info['rollover']
        
        return {
            'client': client_info['name'],
            'clientCode': client_code,
            'period': period_label,
            'budget': budget,
            'spent': spent,
            'remaining': budget - spent,
            'percentUsed': round((spent / budget * 100) if budget > 0 else 0),
            'rolloverApplied': client_info['rolloverUse'] == quarter_key and client_info['rollover'] > 0,
            'rolloverAmount': client_info['rollover'] if client_info['rolloverUse'] == quarter_key else 0
        }
    
    except Exception as e:
        return {'error': str(e)}


# Tool definitions for Claude
CLAUDE_TOOLS = [
    {
        "name": "search_people",
        "description": "Search for contacts/people in the database. Use this when asked about client contacts, email addresses, phone numbers, or how many people work at a client.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_code": {
                    "type": "string",
                    "description": "Filter by client code (e.g., 'SKY', 'TOW', 'ONE'). Optional."
                },
                "search_term": {
                    "type": "string",
                    "description": "Search for a specific person by name or email. Optional."
                }
            },
            "required": []
        }
    },
    {
        "name": "get_client_detail",
        "description": "Get detailed information about a client including their budget, quarter, and commercial setup. Use this when asked about a client's retainer, budget, or financial setup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_code": {
                    "type": "string",
                    "description": "The client code (e.g., 'SKY', 'TOW', 'ONE')"
                }
            },
            "required": ["client_code"]
        }
    },
    {
        "name": "get_spend_summary",
        "description": "Get spend/budget summary for a client. Use this when asked about how much has been spent, budget remaining, or financial tracking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_code": {
                    "type": "string",
                    "description": "The client code (e.g., 'SKY', 'TOW', 'ONE')"
                },
                "period": {
                    "type": "string",
                    "description": "Time period: 'this_month', 'this_quarter', 'Q1', 'Q2', 'Q3', 'Q4', or a month name like 'January'"
                }
            },
            "required": ["client_code"]
        }
    }
]


def execute_tool(tool_name, tool_input):
    """Execute a tool and return results"""
    if tool_name == "search_people":
        return tool_search_people(
            client_code=tool_input.get('client_code'),
            search_term=tool_input.get('search_term')
        )
    elif tool_name == "get_client_detail":
        return tool_get_client_detail(tool_input.get('client_code'))
    elif tool_name == "get_spend_summary":
        return tool_get_spend_summary(
            client_code=tool_input.get('client_code'),
            period=tool_input.get('period', 'this_month')
        )
    else:
        return {'error': f'Unknown tool: {tool_name}'}


# ===== CLAUDE PARSE (Query Understanding with Memory and Tools) =====
@app.route('/claude/parse', methods=['POST'])
def claude_parse():
    """
    Parse a natural language query using Claude.
    Now supports tool use for data lookups.
    Returns structured intent for the frontend to execute.
    """
    data = request.get_json()
    question = data.get('question', '')
    clients = data.get('clients', [])
    session_id = data.get('sessionId', 'default')
    user_mode = data.get('userMode', 'hunch')  # 'hunch' or 'client'
    user_client = data.get('userClient')  # client code if mode is 'client'
    tracker_access = data.get('trackerAccess', True)
    
    if not question:
        return jsonify({'error': 'No question provided'}), 400
    
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Anthropic API not configured'}), 500
    
    try:
        # Get conversation history and context
        conv = get_conversation(session_id)
        history = conv['messages']
        context = conv['context']
        
        # Build client list for prompt
        client_list = ', '.join([f"{c['code']} ({c['name']})" for c in clients])
        
        # Build context hint
        context_hint = ""
        if context.get('lastClient'):
            context_hint += f"Last discussed client: {context['lastClient']}. "
        if context.get('lastJob'):
            context_hint += f"Last discussed job: {context['lastJob']}. "
        
        # Permission context
        permission_hint = ""
        if user_mode == 'client':
            permission_hint = f"User is a CLIENT viewing as {user_client}. Only show them data for their client. "
            if not tracker_access:
                permission_hint += "They do NOT have access to financial/tracker data. "
        
        system_prompt = f"""You are Dot, the admin-bot for Hunch creative agency.

WHO YOU ARE:
A helpful, fun colleague who happens to be a robot. Warm, quick, occasionally cheeky - but always genuinely trying to help. Think friendly coworker with perfect memory and access to all the data.

When someone asks you something, your first instinct is "how can I help?" not "is this allowed?"

WHAT YOU KNOW:
You have access to Hunch's Airtable database:
- PROJECTS: Job number, name, description, stage, status, due dates, updates, Teams links
- CLIENTS: Client name, code, Teams IDs
- PEOPLE: Contact names, emails, phone numbers
- TRACKER: Budget, spend, and numbers by client and quarter

You can search, filter, sort, and retrieve any of this information.

AVAILABLE CLIENTS: {client_list}

These are company names:
- "Sky" = SKY (Sky TV) - never the weather
- "One" / "One NZ" = THREE separate client codes: ONE (Marketing), ONB (Business), ONS (Simplification). When someone asks about "One NZ" contacts or people, search ALL THREE codes and combine the results. Ask for clarification only if they specifically need one division.
- "Tower" = TOW (Tower Insurance) - never a building
- "Fisher" = FIS (Fisher Funds)

CONVERSATION CONTEXT: {context_hint if context_hint else 'Fresh conversation.'}
{permission_hint}

TOOLS:
You have access to tools that can look up data. Use them when:
- Asked about contacts/people (search_people)
- Asked about client details/setup (get_client_detail)
- Asked about spend/budget that isn't in the preloaded data (get_spend_summary)

The frontend already has Projects preloaded, so for job queries, just return the intent - don't use tools.

CONFIDENCE:
- If you can answer from context or the question is about jobs (which are preloaded), answer directly
- If you need data you don't have (people, detailed client info, specific spend queries), use a tool
- If you're not sure you have the right data, say so and offer to dig deeper

RESPONSE FORMAT:
Return ONLY valid JSON:
{{"coreRequest": "FIND" | "DUE" | "UPDATE" | "TRACKER" | "HELP" | "CLARIFY" | "QUERY" | "HANDOFF" | "LOG" | "UNKNOWN", "modifiers": {{"client": "CLIENT_CODE or null", "status": "In Progress" | "On Hold" | "Incoming" | "Completed" | null, "withClient": true | false | null, "dateRange": "today" | "tomorrow" | "week" | "next" | null, "period": "this_month" | "last_month" | "January" | "February" | ... | "Q1" | "Q2" | "Q3" | "Q4" | "this_quarter" | "last_quarter" | null, "sortBy": "dueDate" | "updated" | null, "sortOrder": "asc" | "desc" | null}}, "searchTerms": [], "queryType": "contact | details | null", "queryTarget": "who or what to look up", "understood": true | false, "responseText": "What Dot says - warm and fun", "nextPrompt": "One short followup 4-6 words or null", "handoffQuestion": "original question for HANDOFF", "logTitle": "short description or null", "logNotes": "conversation context or null"}}

REQUEST TYPES:
- FIND: Looking for jobs ("Show me Sky jobs", "What's on hold?")
- DUE: Deadline queries ("What's due today?", "What's overdue?")
- QUERY: Data lookups - USE TOOLS for these ("Who's our contact at Fisher?", "What's Sarah's email?", "How many people at Tower?")
- TRACKER: Budget/spend/numbers queries. Examples:
  - "How's Tower tracking?" → TRACKER, client: TOW, period: this_month
  - "Where did One NZ land last month?" → TRACKER, client: ONE, period: last_month
  - "Sky's Q4 numbers" → TRACKER, client: SKY, period: Q4
  - "This quarter's spend for Fisher" → TRACKER, client: FIS, period: this_quarter
  If no period specified, default to this_month.
- UPDATE: Wants to update a job
- LOG: User wants to log a bug or feature. Triggers: "log this", "add to the bug list", "add to the feature list", "note this bug", "note this feature", "what's on the bug list?", "what features are logged?"
  For logging: set logTitle (short description) and logNotes (what we were discussing)
  For reading: just return LOG with responseText summarising what you'll fetch
- HELP: Wants to know what Dot can do
- CLARIFY: User said "them/that" but no context - ask who they mean
- HANDOFF: Something that genuinely needs a human

RESPONSE TEXT:
This is what the user sees. Make it warm, natural, and fun.
Good: "Here's what's on for Sky:" / "Found it!" / "3 jobs due today - let's get after them:"
Bad: "I found the following jobs:" / "Based on your query:"

If you used a tool to get data, incorporate that data naturally into your responseText.

WHEN YOU CANNOT HELP:
If outside your scope, set understood to false and write a warm fallback.
Good: "Ha, I wish! I only know Hunch stuff." / "That's beyond my robot brain, sorry!"

If a human at Hunch could help, use HANDOFF with responseText "That's a question for a human..."

CLARIFY:
If someone says "them" or "that client" but no context, set coreRequest to CLARIFY and responseText to "Remind me, which client?"

NEXT PROMPT:
Suggest ONE helpful followup as Dot offering to help - warm, conversational, not a button label.
Good: "Want me to check what's due?" / "I can dig into a specific client if you like" / "Any particular job you're after?"
Bad: "Show projects" / "Check deadlines" / "View client"
Keep it short (under 10 words) or null if nothing fits naturally.

REMEMBER: You're helpful first. Most questions have a yes answer. Find it."""

        # Build messages with history
        messages = []
        for msg in history[-10:]:  # Last 5 exchanges
            messages.append(msg)
        messages.append({'role': 'user', 'content': question})
        
        # Determine if we should use tools
        # Keywords that suggest tool use might be needed
        tool_keywords = ['contact', 'email', 'phone', 'people', 'person', 'how many', 
                        'who is', "who's", 'invite', 'birthday', 'address']
        might_need_tools = any(kw in question.lower() for kw in tool_keywords)
        
        # First API call - with or without tools
        api_params = {
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 1000,
            'system': system_prompt,
            'messages': messages
        }
        
        if might_need_tools:
            # Filter tools based on user permissions
            available_tools = CLAUDE_TOOLS.copy()
            if not tracker_access:
                available_tools = [t for t in available_tools if t['name'] != 'get_spend_summary']
            if user_mode == 'client' and user_client:
                # Client users can only query their own client
                # We'll enforce this in tool execution
                pass
            
            api_params['tools'] = available_tools
        
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json'
            },
            json=api_params
        )
        
        response.raise_for_status()
        result = response.json()
        
        # Check if Claude wants to use tools
        stop_reason = result.get('stop_reason')
        content_blocks = result.get('content', [])
        
        if stop_reason == 'tool_use':
            # Claude wants to use a tool
            tool_results = []
            
            for block in content_blocks:
                if block.get('type') == 'tool_use':
                    tool_name = block.get('name')
                    tool_input = block.get('input', {})
                    tool_id = block.get('id')
                    
                    # Enforce client restrictions
                    if user_mode == 'client' and user_client:
                        if 'client_code' in tool_input:
                            tool_input['client_code'] = user_client
                        elif tool_name in ['get_client_detail', 'get_spend_summary']:
                            tool_input['client_code'] = user_client
                    
                    # Execute the tool
                    print(f"Executing tool: {tool_name} with input: {tool_input}")
                    tool_result = execute_tool(tool_name, tool_input)
                    print(f"Tool result: {tool_result}")
                    
                    tool_results.append({
                        'type': 'tool_result',
                        'tool_use_id': tool_id,
                        'content': json.dumps(tool_result)
                    })
            
            # Second API call with tool results
            messages.append({'role': 'assistant', 'content': content_blocks})
            
            # Add tool results with a reminder to return JSON
            tool_results_with_reminder = tool_results + [{
                'type': 'text',
                'text': 'Now respond with ONLY valid JSON in the required format. No other text.'
            }]
            messages.append({'role': 'user', 'content': tool_results_with_reminder})
            
            response2 = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'x-api-key': ANTHROPIC_API_KEY,
                    'anthropic-version': '2023-06-01',
                    'content-type': 'application/json'
                },
                json={
                    'model': 'claude-sonnet-4-20250514',
                    'max_tokens': 1000,
                    'system': system_prompt,
                    'messages': messages
                }
            )
            
            response2.raise_for_status()
            result = response2.json()
            content_blocks = result.get('content', [])
        
        # Extract the text response
        assistant_message = ''
        for block in content_blocks:
            if block.get('type') == 'text':
                assistant_message = block.get('text', '')
                break
        
        # Try to parse as JSON
        try:
            # Clean up response - sometimes Claude adds markdown
            clean_message = assistant_message.strip()
            if clean_message.startswith('```'):
                clean_message = clean_message.split('```')[1]
                if clean_message.startswith('json'):
                    clean_message = clean_message[4:]
            clean_message = clean_message.strip()
            
            parsed = json.loads(clean_message)
            
            # Update conversation history
            add_to_conversation(session_id, 'user', question)
            add_to_conversation(session_id, 'assistant', f"Parsed: {parsed.get('coreRequest')} for {parsed.get('modifiers', {}).get('client', 'no client')}")
            
            # Update context
            if parsed.get('modifiers', {}).get('client'):
                update_context(session_id, {'lastClient': parsed['modifiers']['client']})
            
            return jsonify({'parsed': parsed})
            
        except json.JSONDecodeError as e:
            print(f'JSON parse error: {e}')
            print(f'Raw response: {assistant_message}')
            return jsonify({'parsed': None, 'error': 'Could not parse response'})
    
    except Exception as e:
        print(f'Error calling Claude: {e}')
        return jsonify({'error': str(e)}), 500


# ===== CLEAR SESSION =====
@app.route('/claude/clear', methods=['POST'])
def clear_session():
    """Clear conversation history for a session"""
    data = request.get_json()
    session_id = data.get('sessionId', 'default')
    
    if session_id in conversations:
        del conversations[session_id]
    
    return jsonify({'success': True})


# ===== FEATURE LOG =====
@app.route('/log', methods=['GET'])
def get_features():
    """Get the awesomer list"""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Features%20and%20bugs"
    
    response = requests.get(url, headers=HEADERS)
    
    if response.status_code != 200:
        return jsonify({'error': 'Failed to fetch'}), 500
    
    records = response.json().get('records', [])
    
    items = []
    for r in records:
        fields = r.get('fields', {})
        items.append({
            'title': fields.get('Title', ''),
            'notes': fields.get('Notes', ''),
            'done': fields.get('Done', False)
        })
    
    return jsonify({'items': items})


@app.route('/log', methods=['POST'])
def log_feature():
    """Log a feature request or bug to the awesomer list"""
    data = request.json
    
    if not data or not data.get('title'):
        return jsonify({'error': 'Title required'}), 400
    
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Features%20and%20bugs"
    
    fields = {
        'Title': data['title']
    }
    
    if data.get('notes'):
        fields['Notes'] = data['notes']
    
    response = requests.post(
        url,
        headers=HEADERS,
        json={'fields': fields}
    )
    
    if response.status_code == 200:
        return jsonify({
            'success': True,
            'message': f"Logged: {data['title']}"
        })
    else:
        return jsonify({'error': 'Failed to log'}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
