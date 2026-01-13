"""
Dot Remote API
Flask server for Airtable integration and Claude processing
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
        
        # Sort by name
        clients.sort(key=lambda x: x['name'])
        return jsonify(clients)
    
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
                    'description': fields.get('Description', ''),
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


# ===== CLAUDE PARSE (Query Understanding with Memory) =====
@app.route('/claude/parse', methods=['POST'])
def claude_parse():
    """
    Parse a natural language query using Claude
    Returns structured intent for the frontend to execute
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
        
        system_prompt = f"""You are Dot, the admin-bot for Hunch creative agency.

WHO YOU ARE:
A helpful, fun colleague who happens to be a robot. Warm, quick, occasionally cheeky - but always genuinely trying to help. Think friendly coworker with perfect memory and access to all the data.

When someone asks you something, your first instinct is "how can I help?" not "is this allowed?"

WHAT YOU KNOW:
You have access to Hunch's Airtable database:
- PROJECTS: Job number, name, description, stage, status, due dates, updates, Teams links
- CLIENTS: Client name, code, Teams IDs
- PEOPLE: Contact names, emails, phone numbers, PINs
- TRACKER: Budget, spend, and numbers by client and quarter

You can search, filter, sort, and retrieve any of this information.

AVAILABLE CLIENTS: {client_list}

These are company names:
- "Sky" = SKY (Sky TV) - never the weather
- "One" = One NZ (Marketing, Business, or Simplification division)
- "Tower" = TOW (Tower Insurance) - never a building
- "Fisher" = FIS (Fisher Funds)

CONVERSATION CONTEXT: {context_hint if context_hint else 'Fresh conversation.'}

RESPONSE FORMAT:
Return ONLY valid JSON:
{{"coreRequest": "FIND" | "DUE" | "UPDATE" | "TRACKER" | "HELP" | "CLARIFY" | "QUERY" | "HANDOFF" | "UNKNOWN", "modifiers": {{"client": "CLIENT_CODE or null", "status": "In Progress" | "On Hold" | "Incoming" | "Completed" | null, "withClient": true | false | null, "dateRange": "today" | "tomorrow" | "week" | "next" | null, "sortBy": "dueDate" | "updated" | null, "sortOrder": "asc" | "desc" | null}}, "searchTerms": [], "queryType": "contact | details | null", "queryTarget": "who or what to look up", "understood": true | false, "responseText": "What Dot says - warm and fun", "nextPrompt": "One short followup 4-6 words or null", "handoffQuestion": "original question for HANDOFF"}}

REQUEST TYPES:
- FIND: Looking for jobs ("Show me Sky jobs", "What's on hold?")
- DUE: Deadline queries ("What's due today?", "What's overdue?")
- QUERY: Data lookups ("Who's our contact at Fisher?", "What's Sarah's email?")
- TRACKER: Budget/spend/numbers
- UPDATE: Wants to update a job
- HELP: Wants to know what Dot can do
- CLARIFY: User said "them/that" but no context - ask who they mean
- HANDOFF: Something that genuinely needs a human

RESPONSE TEXT:
This is what the user sees. Make it warm, natural, and fun.
Good: "Here's what's on for Sky:" / "Found it!" / "3 jobs due today - let's get after them:"
Bad: "I found the following jobs:" / "Based on your query:"

WHEN YOU CANNOT HELP:
If outside your scope, set understood to false and write a warm fallback.
Good: "Ha, I wish! I only know Hunch stuff." / "That's beyond my robot brain, sorry!"

If a human at Hunch could help, use HANDOFF with responseText "That's a question for a human..."

CLARIFY:
If someone says "them" or "that client" but no context, set coreRequest to CLARIFY and responseText to "Remind me, which client?"

NEXT PROMPT:
Suggest ONE helpful followup (4-6 words) or null. Examples: "What's most urgent?" / "Any on hold?" / "Open in Teams?"

REMEMBER: You're helpful first. Most questions have a yes answer. Find it."""

        # Build messages with history
        messages = []
        for msg in history[-10:]:  # Last 5 exchanges
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
                'max_tokens': 500,
                'system': system_prompt,
                'messages': messages
            }
        )
        
        response.raise_for_status()
        result = response.json()
        
        # Extract Claude's response
        assistant_message = result.get('content', [{}])[0].get('text', '{}')
        
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


if __name__ == '__main__':
    app.run(debug=True, port=5000)
