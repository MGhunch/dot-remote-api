"""
Dot Traffic 2.0 - Airtable Operations
All reads and writes to Airtable: Projects, Clients, Traffic table
"""

import os
import re
import httpx
from datetime import datetime

# ===================
# CONFIG
# ===================

AIRTABLE_API_KEY = os.environ.get('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID', 'app8CI7NAZqhQ4G1Y')

PROJECTS_TABLE = 'Projects'
CLIENTS_TABLE = 'Clients'
TRAFFIC_TABLE = 'Traffic'
UPDATES_TABLE = 'Updates'

TIMEOUT = 10.0


# ===================
# DATE PARSING HELPERS (same as dot-hub-api)
# ===================

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


def _headers():
    """Standard Airtable headers"""
    return {
        'Authorization': f'Bearer {AIRTABLE_API_KEY}',
        'Content-Type': 'application/json'
    }


def _url(table):
    """Build Airtable URL for a table"""
    return f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}'


# ===================
# TRAFFIC TABLE (Deduplication & Logging)
# ===================

def check_duplicate(internet_message_id):
    """
    Check if we've already processed this email.
    Returns the existing record if found, None otherwise.
    """
    if not AIRTABLE_API_KEY or not internet_message_id:
        return None
    
    try:
        params = {
            'filterByFormula': f"{{internetMessageId}}='{internet_message_id}'"
        }
        
        response = httpx.get(
            _url(TRAFFIC_TABLE), 
            headers=_headers(), 
            params=params, 
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        return records[0] if records else None
        
    except Exception as e:
        print(f"[airtable] Error checking duplicate: {e}")
        return None


def check_pending_clarify(conversation_id):
    """
    Check if this conversation has a pending clarify request.
    Returns the pending record if found, None otherwise.
    """
    if not AIRTABLE_API_KEY or not conversation_id:
        return None
    
    try:
        filter_formula = f"AND({{conversationId}}='{conversation_id}', {{Status}}='pending')"
        params = {'filterByFormula': filter_formula}
        
        response = httpx.get(
            _url(TRAFFIC_TABLE), 
            headers=_headers(), 
            params=params, 
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        return records[0] if records else None
        
    except Exception as e:
        print(f"[airtable] Error checking pending clarify: {e}")
        return None


def log_traffic(internet_message_id, conversation_id, route, status, job_number, client_code, sender_email, subject):
    """
    Log email to Traffic table.
    Returns the created record ID or None.
    """
    if not AIRTABLE_API_KEY:
        return None
    
    try:
        record_data = {
            'fields': {
                'internetMessageId': internet_message_id or '',
                'conversationId': conversation_id or '',
                'Route': route,
                'Status': status,
                'JobNumber': job_number or '',
                'clientCode': client_code or '',
                'SenderEmail': sender_email or '',
                'Subject': subject or '',
                'CreatedAt': datetime.utcnow().isoformat()
            }
        }
        
        response = httpx.post(
            _url(TRAFFIC_TABLE), 
            headers=_headers(), 
            json=record_data, 
            timeout=TIMEOUT
        )
        
        if response.status_code != 200:
            print(f"[airtable] Traffic log rejected: {response.status_code} - {response.text}")
            return None
        
        return response.json().get('id')
        
    except Exception as e:
        print(f"[airtable] Error logging to Traffic: {e}")
        return None


def update_traffic_record(record_id, updates):
    """
    Update an existing Traffic table record.
    updates: dict of field names to values
    """
    if not AIRTABLE_API_KEY or not record_id:
        return False
    
    try:
        response = httpx.patch(
            f"{_url(TRAFFIC_TABLE)}/{record_id}",
            headers=_headers(),
            json={'fields': updates},
            timeout=TIMEOUT
        )
        response.raise_for_status()
        return True
        
    except Exception as e:
        print(f"[airtable] Error updating Traffic record: {e}")
        return False


# ===================
# PROJECTS TABLE
# ===================

def get_project(job_number):
    """
    Look up project by job number.
    Returns dict with project info or None.
    """
    if not AIRTABLE_API_KEY or not job_number:
        return None
    
    try:
        params = {
            'filterByFormula': f"{{Job Number}}='{job_number}'"
        }
        
        response = httpx.get(
            _url(PROJECTS_TABLE), 
            headers=_headers(), 
            params=params, 
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return None
        
        record = records[0]
        fields = record['fields']
        
        # Client name might be a linked field (list)
        client_name = fields.get('Client', '')
        if isinstance(client_name, list):
            client_name = client_name[0] if client_name else ''
        
        # Extract client code from job number
        client_code = job_number.split()[0] if job_number else None
        
        # Get Team ID from Clients table
        team_id = get_team_id(client_code) if client_code else None
        
        return {
            'recordId': record['id'],
            'jobNumber': fields.get('Job Number', job_number),
            'jobName': fields.get('Project Name', ''),
            'clientName': client_name,
            'clientCode': client_code,
            'stage': fields.get('Stage', ''),
            'status': fields.get('Status', ''),
            'round': fields.get('Round', 0) or 0,
            'withClient': fields.get('With Client?', False),
            'teamsChannelId': fields.get('Teams Channel ID', None),
            'teamId': team_id
        }
        
    except Exception as e:
        print(f"[airtable] Error looking up project: {e}")
        return None


def get_active_jobs(client_code):
    """
    Get all active (not completed) jobs for a client.
    Returns list of job dicts with full details for job cards.
    """
    if not AIRTABLE_API_KEY or not client_code:
        return []
    
    try:
        # Get all jobs that are NOT completed
        filter_formula = f"AND(FIND('{client_code}', {{Job Number}})=1, {{Status}}!='Completed')"
        params = {'filterByFormula': filter_formula}
        
        print(f"[airtable] Fetching active jobs for {client_code}")
        
        response = httpx.get(
            _url(PROJECTS_TABLE), 
            headers=_headers(), 
            params=params, 
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        
        print(f"[airtable] Found {len(records)} active jobs for {client_code}")
        
        jobs = []
        for record in records:
            fields = record.get('fields', {})
            job_number = fields.get('Job Number', '')
            
            # Get update text - same as dot-hub-api
            update_summary = fields.get('Update Summary', '') or fields.get('Update', '')
            latest_update = update_summary
            if '|' in update_summary:
                parts = update_summary.split('|')
                latest_update = parts[-1].strip() if parts else update_summary
            
            # Parse dates - same as dot-hub-api
            update_due_friendly = fields.get('Update due friendly', '')
            update_due = parse_friendly_date(update_due_friendly)
            
            live_date_raw = fields.get('Live Date', '')
            live_date = parse_friendly_date(live_date_raw) if live_date_raw else None
            
            last_update_made = fields.get('Last update made', '')
            last_updated = parse_status_changed(last_update_made)
            
            # Get update history - could be array or string
            update_history_raw = fields.get('Update history', [])
            if isinstance(update_history_raw, str):
                update_history = [u.strip() for u in update_history_raw.split('\n') if u.strip()]
            elif isinstance(update_history_raw, list):
                update_history = update_history_raw[:5]
            else:
                update_history = []
            
            jobs.append({
                'jobNumber': job_number,
                'jobName': fields.get('Project Name', ''),
                'description': fields.get('Description', ''),
                'stage': fields.get('Stage', ''),
                'status': fields.get('Status', ''),
                'updateDue': update_due,
                'withClient': fields.get('With Client?', False),
                'clientCode': job_number.split()[0] if job_number else '',
                'update': latest_update,
                'lastUpdated': last_updated,
                'updateHistory': update_history,
                'liveDate': live_date,
            })
        
        return jobs
        
    except Exception as e:
        print(f"[airtable] Error getting active jobs: {e}")
        return []


def get_all_active_jobs():
    """
    Get ALL active jobs across ALL clients.
    Returns list of job dicts - typically ~20 jobs total.
    Use this for cross-client queries like "What's due today?"
    """
    if not AIRTABLE_API_KEY:
        return []
    
    try:
        # Get all jobs that are NOT completed
        filter_formula = "{Status}!='Completed'"
        params = {'filterByFormula': filter_formula}
        
        print(f"[airtable] Fetching all active jobs across all clients")
        
        response = httpx.get(
            _url(PROJECTS_TABLE), 
            headers=_headers(), 
            params=params, 
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        
        print(f"[airtable] Found {len(records)} total active jobs")
        
        jobs = []
        for record in records:
            fields = record.get('fields', {})
            job_number = fields.get('Job Number', '')
            
            # Get update text - same as dot-hub-api
            update_summary = fields.get('Update Summary', '') or fields.get('Update', '')
            latest_update = update_summary
            if '|' in update_summary:
                parts = update_summary.split('|')
                latest_update = parts[-1].strip() if parts else update_summary
            
            # Parse dates - same as dot-hub-api
            update_due_friendly = fields.get('Update due friendly', '')
            update_due = parse_friendly_date(update_due_friendly)
            
            live_date_raw = fields.get('Live Date', '')
            live_date = parse_friendly_date(live_date_raw) if live_date_raw else None
            
            last_update_made = fields.get('Last update made', '')
            last_updated = parse_status_changed(last_update_made)
            
            # Get update history - could be array or string
            update_history_raw = fields.get('Update history', [])
            if isinstance(update_history_raw, str):
                update_history = [u.strip() for u in update_history_raw.split('\n') if u.strip()]
            elif isinstance(update_history_raw, list):
                update_history = update_history_raw[:5]
            else:
                update_history = []
            
            jobs.append({
                'jobNumber': job_number,
                'jobName': fields.get('Project Name', ''),
                'description': fields.get('Description', ''),
                'stage': fields.get('Stage', ''),
                'status': fields.get('Status', ''),
                'updateDue': update_due,
                'withClient': fields.get('With Client?', False),
                'clientCode': job_number.split()[0] if job_number else '',
                'update': latest_update,
                'lastUpdated': last_updated,
                'updateHistory': update_history,
                'liveDate': live_date,
            })
        
        return jobs
        
    except Exception as e:
        print(f"[airtable] Error getting all active jobs: {e}")
        return []


def get_job_by_number(job_number):
    """
    Get a specific job by its job number (e.g., 'LAB 055').
    Returns job dict or None if not found.
    """
    if not AIRTABLE_API_KEY or not job_number:
        return None
    
    try:
        # Normalize job number format (LAB_055 -> LAB 055)
        job_number = job_number.replace('_', ' ').upper()
        
        filter_formula = f"{{Job Number}}='{job_number}'"
        params = {
            'filterByFormula': filter_formula,
            'maxRecords': 1
        }
        
        print(f"[airtable] Fetching job: {job_number}")
        
        response = httpx.get(
            _url(PROJECTS_TABLE), 
            headers=_headers(), 
            params=params, 
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        
        if not records:
            print(f"[airtable] Job {job_number} not found")
            return None
        
        fields = records[0].get('fields', {})
        
        # Get update text - same as dot-hub-api
        update_summary = fields.get('Update Summary', '') or fields.get('Update', '')
        latest_update = update_summary
        if '|' in update_summary:
            parts = update_summary.split('|')
            latest_update = parts[-1].strip() if parts else update_summary
        
        # Parse dates - same as dot-hub-api
        update_due_friendly = fields.get('Update due friendly', '')
        update_due = parse_friendly_date(update_due_friendly)
        
        live_date_raw = fields.get('Live Date', '')
        live_date = parse_friendly_date(live_date_raw) if live_date_raw else None
        
        last_update_made = fields.get('Last update made', '')
        last_updated = parse_status_changed(last_update_made)
        
        # Get update history - could be array or string
        update_history_raw = fields.get('Update history', [])
        if isinstance(update_history_raw, str):
            update_history = [u.strip() for u in update_history_raw.split('\n') if u.strip()]
        elif isinstance(update_history_raw, list):
            update_history = update_history_raw[:5]
        else:
            update_history = []
        
        return {
            'jobNumber': fields.get('Job Number', ''),
            'jobName': fields.get('Project Name', ''),
            'description': fields.get('Description', ''),
            'stage': fields.get('Stage', ''),
            'status': fields.get('Status', ''),
            'updateDue': update_due,
            'withClient': fields.get('With Client?', False),
            'clientCode': job_number.split()[0] if job_number else '',
            'update': latest_update,
            'lastUpdated': last_updated,
            'updateHistory': update_history,
            'liveDate': live_date,
        }
        
    except Exception as e:
        print(f"[airtable] Error getting job by number: {e}")
        return None


def update_project_record(job_number, updates):
    """
    Update a project's fields by job number.
    Used by Hub card-update (modal) for direct field updates.
    
    Args:
        job_number: e.g., 'LAB 055'
        updates: dict of Airtable field names to values, e.g.:
            {
                'Stage': 'Craft',
                'Status': 'In Progress',
                'With Client?': True,
                'Update Due': '2026-01-25',
                'Live Date': '2026-02-15',
                'Description': 'Updated description',
                'Project Owner': 'Sarah',
                'Project Name': 'New name'
            }
    
    Returns:
        dict with 'success': True/False and 'updated': list of field names
    """
    if not AIRTABLE_API_KEY or not job_number:
        return {'success': False, 'error': 'Missing API key or job number'}
    
    try:
        # Find the project record
        params = {
            'filterByFormula': f"{{Job Number}}='{job_number}'",
            'maxRecords': 1
        }
        
        response = httpx.get(
            _url(PROJECTS_TABLE),
            headers=_headers(),
            params=params,
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return {'success': False, 'error': f'Job {job_number} not found'}
        
        record_id = records[0]['id']
        
        # Update the record
        response = httpx.patch(
            f"{_url(PROJECTS_TABLE)}/{record_id}",
            headers=_headers(),
            json={'fields': updates},
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        print(f"[airtable] Updated project {job_number}: {list(updates.keys())}")
        return {'success': True, 'updated': list(updates.keys())}
        
    except Exception as e:
        print(f"[airtable] Error updating project record: {e}")
        return {'success': False, 'error': str(e)}


def create_update_record(job_number, update_text, update_due=None):
    """
    Create a new record in the Updates table.
    
    Args:
        job_number: e.g., 'LAB 055' - used to link to project
        update_text: The update message
        update_due: Optional due date (ISO format)
    
    Returns:
        dict with 'success': True/False and 'record_id' if successful
    """
    if not AIRTABLE_API_KEY or not job_number or not update_text:
        return {'success': False, 'error': 'Missing required fields'}
    
    try:
        # First, find the project record ID to link to
        params = {
            'filterByFormula': f"{{Job Number}}='{job_number}'",
            'maxRecords': 1
        }
        
        response = httpx.get(
            _url(PROJECTS_TABLE),
            headers=_headers(),
            params=params,
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return {'success': False, 'error': f'Project {job_number} not found'}
        
        project_record_id = records[0]['id']
        
        # Build the Updates record
        update_fields = {
            'Update': update_text,
            'Project Link': [project_record_id]  # Linked record field
        }
        
        if update_due:
            update_fields['Update due'] = update_due
        
        # Create the record
        response = httpx.post(
            _url(UPDATES_TABLE),
            headers=_headers(),
            json={'fields': update_fields},
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        new_record = response.json()
        print(f"[airtable] Created update record for {job_number}: {new_record.get('id')}")
        
        return {'success': True, 'record_id': new_record.get('id')}
        
    except Exception as e:
        print(f"[airtable] Error creating update record: {e}")
        return {'success': False, 'error': str(e)}


# ===================
# CLIENTS TABLE
# ===================

def get_team_id(client_code):
    """
    Look up Team ID from Clients table by client code.
    Returns Team ID string or None.
    """
    if not AIRTABLE_API_KEY or not client_code:
        return None
    
    try:
        params = {
            'filterByFormula': f"{{Client code}}='{client_code}'"
        }
        
        response = httpx.get(
            _url(CLIENTS_TABLE), 
            headers=_headers(), 
            params=params, 
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return None
        
        return records[0]['fields'].get('Teams ID', None)
        
    except Exception as e:
        print(f"[airtable] Error looking up Team ID: {e}")
        return None


def get_client_name(client_code):
    """
    Look up client name from Clients table by client code.
    Returns client name string or None.
    """
    if not AIRTABLE_API_KEY or not client_code:
        return None
    
    try:
        params = {
            'filterByFormula': f"{{Client code}}='{client_code}'"
        }
        
        response = httpx.get(
            _url(CLIENTS_TABLE), 
            headers=_headers(), 
            params=params, 
            timeout=TIMEOUT
        )
        response.raise_for_status()
        
        records = response.json().get('records', [])
        if not records:
            return None
        
        return records[0]['fields'].get('Clients', None)
        
    except Exception as e:
        print(f"[airtable] Error looking up client name: {e}")
        return None
