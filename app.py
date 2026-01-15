"""
Dot Remote API
Flask server for Airtable integration and Claude processing
Simplified: Claude responds naturally, frontend handles job display
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


# ===== JOBS =====
@app.route('/jobs/all')
def get_all_jobs():
    """Get all active jobs for WIP board and Ask Dot"""
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
        
        all_records.sort(key=lambda x: x['updateDue'] or '9999-99-99')
        return jsonify(all_records)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/job/<job_number>/update', methods=['POST'])
def update_job(job_number):
    """Update a job's fields"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
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
    """Get clients with committed retainer budgets"""
    try:
        url = get_airtable_url('Clients')
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        clients = []
        
        for record in records:
            fields = record.get('fields', {})
            
            monthly_committed = fields.get('Monthly Committed', 0)
            if isinstance(monthly_committed, str):
                monthly_committed = int(monthly_committed.replace('$', '').replace(',', '') or 0)
            
            rollover_credit = fields.get('Rollover Credit', 0)
            if isinstance(rollover_credit, list):
                rollover_credit = rollover_credit[0] if rollover_credit else 0
            if isinstance(rollover_credit, str):
                rollover_credit = int(rollover_credit.replace('$', '').replace(',', '') or 0)
            
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
        
        clients = [c for c in clients if c['committed'] > 0]
        clients.sort(key=lambda x: x['name'])
        return jsonify(clients)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/tracker/data')
def get_tracker_data():
    """Get tracker data for a specific client"""
    client_code = request.args.get('client')
    if not client_code:
        return jsonify({'error': 'client parameter required'}), 400
    
    try:
        # Get Projects for this client
        projects_url = get_airtable_url('Projects')
        projects_filter = f"SEARCH('{client_code} ', {{Job Number}}) = 1"
        
        projects_by_record_id = {}
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
        
        # Get Tracker records
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
                
                spend = fields.get('Spend', 0)
                if isinstance(spend, str):
                    spend = int(spend.replace('$', '').replace(',', '') or 0)
                
                job_link = fields.get('Job Number', [])
                if isinstance(job_link, list) and len(job_link) > 0:
                    project_record_id = job_link[0]
                else:
                    project_record_id = job_link if isinstance(job_link, str) else None
                
                project = projects_by_record_id.get(project_record_id, {})
                job_number = project.get('jobNumber', '')
                
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
                if key == 'ballpark':
                    airtable_fields[airtable_key] = bool(value)
                else:
                    airtable_fields[airtable_key] = value
        
        if not airtable_fields:
            return jsonify({'error': 'No valid fields to update'}), 400
        
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


# ===== AIRTABLE TOOLS FOR CLAUDE =====

def tool_search_people(client_code=None, search_term=None):
    """Search People table"""
    try:
        url = get_airtable_url('People')
        
        filters = ["{Active} = TRUE()"]
        if client_code:
            filters.append(f"{{Client Link}} = '{client_code}'")
        
        params = {
            'filterByFormula': f"AND({', '.join(filters)})" if len(filters) > 1 else filters[0]
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
    """Get detailed client info"""
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
            'rolloverCredit': rollover,
            'nextJobNumber': fields.get('Next Job #', '')
        }
    
    except Exception as e:
        return {'error': str(e)}


def tool_get_spend_summary(client_code, period='this_month'):
    """Get spend summary for a client"""
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
                    'JAN-MAR': parse_currency(fields.get('JAN-MAR', 0)),
                    'APR-JUN': parse_currency(fields.get('APR-JUN', 0)),
                    'JUL-SEP': parse_currency(fields.get('JUL-SEP', 0)),
                    'OCT-DEC': parse_currency(fields.get('OCT-DEC', 0)),
                    'thisMonth': parse_currency(fields.get('This month', 0)),
                }
                break
        
        if not client_info:
            return {'error': f'Client {client_code} not found'}
        
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


def tool_reserve_job_number(client_code):
    """Reserve the next job number for a client"""
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
        
        record = records[0]
        record_id = record.get('id')
        fields = record.get('fields', {})
        client_name = fields.get('Clients', client_code)
        
        next_num_str = fields.get('Next Job #', '')
        if not next_num_str:
            return {'error': f'No job number sequence configured for {client_code}'}
        
        try:
            next_num = int(next_num_str)
        except ValueError:
            return {'error': f'Invalid job number format: {next_num_str}'}
        
        reserved_job_number = f"{client_code} {next_num:03d}"
        new_next_num = f"{next_num + 1:03d}"
        
        update_response = requests.patch(
            f"{url}/{record_id}",
            headers=HEADERS,
            json={'fields': {'Next Job #': new_next_num}}
        )
        update_response.raise_for_status()
        
        return {
            'success': True,
            'clientCode': client_code,
            'clientName': client_name,
            'reservedJobNumber': reserved_job_number,
            'nextJobNumber': new_next_num
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
        "description": "Get detailed information about a client including their budget, quarter, commercial setup, and next job number.",
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
                    "description": "Time period: 'this_month', 'this_quarter', or 'last_quarter'"
                }
            },
            "required": ["client_code"]
        }
    },
    {
        "name": "reserve_job_number",
        "description": "Reserve and lock in the next job number for a client. This WRITES to the database - only use when the user confirms they want to reserve a number.",
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
    elif tool_name == "reserve_job_number":
        return tool_reserve_job_number(tool_input.get('client_code'))
    else:
        return {'error': f'Unknown tool: {tool_name}'}


# ===== CLAUDE PARSE (Simplified) =====
@app.route('/claude/parse', methods=['POST'])
def claude_parse():
    """
    Parse a natural language query using Claude.
    Simplified: Claude responds naturally, returns job filters when relevant.
    """
    data = request.get_json()
    question = data.get('question', '')
    clients = data.get('clients', [])
    session_id = data.get('sessionId', 'default')
    
    if not question:
        return jsonify({'error': 'No question provided'}), 400
    
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'Anthropic API not configured'}), 500
    
    try:
        conv = get_conversation(session_id)
        history = conv['messages']
        
        client_list = ', '.join([f"{c['code']} ({c['name']})" for c in clients])
        
        system_prompt = f"""You're Dot, the admin bot for Hunch creative agency. You're warm, helpful, occasionally cheeky - a friendly colleague who happens to be a robot with perfect memory.

WHAT YOU KNOW ABOUT:

Jobs/Projects - The frontend has all active jobs preloaded. Each job has:
- Job number (e.g., "SKY 017"), project name, description
- Status: Incoming, In Progress, On Hold, Completed
- Stage: Clarify, Simplify, Craft, Refine, Deliver
- Update due date, live date, last updated
- Whether it's currently "with client" (waiting on them)
- Project owner, Teams channel link

Clients - {client_list}
- "One NZ" has three divisions: ONE (Marketing), ONB (Business), ONS (Simplification). For One NZ people queries, search all three.
- "Sky" = Sky TV, "Tower" = Tower Insurance, "Fisher" = Fisher Funds

People - Contact details for client contacts (names, emails, phone numbers)

Budgets - Each client has a monthly committed spend. You can check:
- How much spent this month/quarter
- How much remaining
- Rollover credits from previous quarters
Talk about "this quarter", "last quarter", "next quarter" - not Q1/Q2/Q3/Q4.

YOUR TOOLS:
- search_people: Find contacts, emails, phone numbers
- get_client_detail: Client setup, budget info, next job number
- get_spend_summary: How much spent/remaining (use period: "this_month", "this_quarter", "last_quarter")
- reserve_job_number: Lock in a job number (CONFIRM WITH USER FIRST - this writes to the database)

For job queries, don't use tools - just return a filter and the frontend will display them.

RESPOND WITH JSON:
{{
  "message": "Your natural response - be yourself, be warm, be helpful",
  "jobs": {{
    "show": true,
    "client": "SKY or null",
    "status": "In Progress | On Hold | Incoming | Completed | null",
    "dateRange": "today | tomorrow | week | null",
    "withClient": true | false | null,
    "search": ["search", "terms"] or null
  }} or null,
  "nextPrompt": "Short followup question or null"
}}

GUIDELINES:
- Only include "jobs" if they're asking about jobs/projects/work
- If you need to clarify, just ask naturally in "message"
- If something needs a human (strategy, creative decisions, opinions), say so warmly - "That's one for the humans!" or "Better to ask the team directly"
- For spend/budget questions, use the tools and include the answer in your message
- Be conversational. Be helpful. Be Dot.

EXAMPLES OF GOOD RESPONSES:
- "Sky's looking healthy this month - $6.2K spent, $3.8K still to play with."
- "Here's what's due this week:" (with jobs filter)
- "Which client are you thinking?"
- "Sarah Chen's email is sarah.chen@sky.co.nz"
- "Ha, that's a bit above my pay grade - one for the humans!"

Don't be robotic. Don't explain what you're doing. Just help."""

        messages = []
        for msg in history[-10:]:
            messages.append(msg)
        messages.append({'role': 'user', 'content': question})
        
        response = requests.post(
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
                'messages': messages,
                'tools': CLAUDE_TOOLS
            }
        )
        
        response.raise_for_status()
        result = response.json()
        
        stop_reason = result.get('stop_reason')
        content_blocks = result.get('content', [])
        
        # Handle tool use
        if stop_reason == 'tool_use':
            tool_results = []
            
            for block in content_blocks:
                if block.get('type') == 'tool_use':
                    tool_name = block.get('name')
                    tool_input = block.get('input', {})
                    tool_id = block.get('id')
                    
                    print(f"Executing tool: {tool_name} with input: {tool_input}")
                    tool_result = execute_tool(tool_name, tool_input)
                    print(f"Tool result: {tool_result}")
                    
                    tool_results.append({
                        'type': 'tool_result',
                        'tool_use_id': tool_id,
                        'content': json.dumps(tool_result)
                    })
            
            messages.append({'role': 'assistant', 'content': content_blocks})
            messages.append({'role': 'user', 'content': tool_results})
            
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
        
        # Extract response
        assistant_message = ''
        for block in content_blocks:
            if block.get('type') == 'text':
                assistant_message = block.get('text', '')
                break
        
        # Parse JSON
        try:
            clean = assistant_message.strip()
            if clean.startswith('```'):
                clean = clean.split('```')[1]
                if clean.startswith('json'):
                    clean = clean[4:]
            clean = clean.strip()
            
            parsed = json.loads(clean)
            
            # Update conversation
            add_to_conversation(session_id, 'user', question)
            add_to_conversation(session_id, 'assistant', parsed.get('message', '')[:100])
            
            return jsonify({'parsed': parsed})
            
        except json.JSONDecodeError as e:
            print(f'JSON parse error: {e}')
            print(f'Raw: {assistant_message}')
            return jsonify({'parsed': {'message': assistant_message, 'jobs': None}})
    
    except Exception as e:
        print(f'Error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/claude/clear', methods=['POST'])
def clear_session():
    """Clear conversation history for a session"""
    data = request.get_json()
    session_id = data.get('sessionId', 'default')
    
    if session_id in conversations:
        del conversations[session_id]
    
    return jsonify({'success': True})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
