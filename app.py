from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import os
from datetime import datetime
import re

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
        'description': fields.get('Description', ''),
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

# Health check
@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'dot-remote-api'})

# Get all clients
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

# Get jobs for a specific client (matches existing format for remote.html)
@app.route('/jobs')
def get_jobs():
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
        
        # Sort by job number
        jobs.sort(key=lambda x: x['id'])
        return jsonify(jobs)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Get single job by job number
@app.route('/job/<job_number>')
def get_job(job_number):
    try:
        url = get_airtable_url('Projects')
        params = {
            'filterByFormula': f"{{Job Number}} = '{job_number}'"
        }
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return jsonify({'error': 'Job not found'}), 404
        
        return jsonify(transform_project(records[0]))
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# NEW: Get ALL active jobs (for To Do / WIP views)
@app.route('/jobs/all')
def get_all_jobs():
    """
    Returns all active jobs (not Completed/Archived) for To Do and WIP views.
    Optional query params:
    - status: filter by specific status
    - include_completed: set to 'true' to include completed jobs
    """
    try:
        include_completed = request.args.get('include_completed', 'false').lower() == 'true'
        status_filter = request.args.get('status')
        
        url = get_airtable_url('Projects')
        
        # Build filter formula
        if status_filter:
            filter_formula = f"{{Status}} = '{status_filter}'"
        elif include_completed:
            filter_formula = "NOT({{Status}} = 'Archived')"
        else:
            filter_formula = "NOT(OR({Status} = 'Completed', {Status} = 'Archived'))"
        
        all_jobs = []
        offset = None
        
        # Handle pagination (Airtable returns max 100 records per request)
        while True:
            params = {'filterByFormula': filter_formula}
            if offset:
                params['offset'] = offset
            
            response = requests.get(url, headers=HEADERS, params=params)
            response.raise_for_status()
            
            data = response.json()
            records = data.get('records', [])
            all_jobs.extend([transform_project(r) for r in records])
            
            offset = data.get('offset')
            if not offset:
                break
        
        # Sort by update due date (soonest first), nulls last
        def sort_key(job):
            if job['updateDue']:
                return (0, job['updateDue'])
            return (1, '9999-99-99')
        
        all_jobs.sort(key=sort_key)
        
        return jsonify(all_jobs)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Update a job field
@app.route('/job/<job_number>/update', methods=['POST'])
def update_job_field(job_number):
    """
    Update specific fields on a job.
    Body: { "field": "value", ... }
    Supported fields: stage, status, withClient, updateDue, liveDate
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # First, find the record ID
        url = get_airtable_url('Projects')
        params = {
            'filterByFormula': f"{{Job Number}} = '{job_number}'"
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
            'withClient': 'With Client?',
            'updateDue': 'Update due',
            'liveDate': 'Live Date'
        }
        
        # Build update payload
        airtable_fields = {}
        for key, value in data.items():
            if key in field_mapping:
                airtable_key = field_mapping[key]
                # Handle withClient boolean -> Airtable checkbox (needs boolean)
                if key == 'withClient':
                    value = bool(value)
                airtable_fields[airtable_key] = value
        
        if not airtable_fields:
            return jsonify({'error': 'No valid fields to update'}), 400
        
        # Update the record
        update_url = f"{url}/{record_id}"
        update_response = requests.patch(
            update_url,
            headers=HEADERS,
            json={'fields': airtable_fields}
        )
        update_response.raise_for_status()
        
        return jsonify({
            'success': True,
            'jobNumber': job_number,
            'updated': list(airtable_fields.keys())
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============ TRACKER ENDPOINTS ============

@app.route('/tracker/clients')
def get_tracker_clients():
    """
    Get clients with tracker-specific fields (committed spend, rollover, year end)
    """
    try:
        url = get_airtable_url('Clients')
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        clients = []
        for record in records:
            fields = record.get('fields', {})
            monthly_committed = fields.get('Monthly Committed', '$0')
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
                    'description': fields.get('Description', ''),
                    'spend': spend,
                    'month': fields.get('Month', ''),
                    'spendType': fields.get('Spend type', 'Project budget'),
                    'ballpark': fields.get('Ballpark') == 'checked',
                    'onUs': fields.get('On us') == 'checked',
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
            'description': 'Description',
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


if __name__ == '__main__':
    app.run(debug=True, port=5000)
