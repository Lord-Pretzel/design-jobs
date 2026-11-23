import re
import json
from datetime import datetime

# Function to extract a date from a description using multiple patterns
def extract_start(description):
    # Combine several common date formats into one regex
    patterns = [
        r'(?i)(?:start|begins|commences|starting)[^\d]*(\d{4}-\d{1,2}-\d{1,2})',  # YYYY-MM-DD
        r'(?i)(?:start|begins|commences|starting)[^\d]*(\d{1,2}/\d{1,2}/\d{4})',   # MM/DD/YYYY or DD/MM/YYYY
        r'(?i)(?:start|begins|commences|starting)[^\d]*(\w+\s\d{1,2}(?:st|nd|rd|th)?\s*\d{4})',  # Month Day, Year
        r'(?i)(?:start|begins|commences|starting)[^\d]*(\d{1,2}\s\w+\s\d{4})',     # Day Month Year
        r'(?i)(?:start|begins|commences|starting)[^\d]*(\w+\s\d{4})',              # Month Year
    ]

    for pattern in patterns:
        match = re.search(pattern, description)
        if match:
            date_str = match.group(1)
            try:
                # Try multiple parsing strategies to handle all formats
                if '-' in date_str and len(date_str.split('-')[0]) == 4:  # YYYY-MM-DD
                    parsed_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                elif '/' in date_str:  # MM/DD/YYYY or DD/MM/YYYY
                    parsed_date = datetime.strptime(date_str, '%m/%d/%Y').date()
                elif re.match(r'\w+\s\d{1,2}(?:st|nd|rd|th)?\s*\d{4}', date_str):  # Month Day, Year
                    parsed_date = datetime.strptime(date_str, '%B %d %Y').date()
                elif re.match(r'\d{1,2}\s\w+\s\d{4}', date_str):  # Day Month Year
                    parsed_date = datetime.strptime(date_str, '%d %B %Y').date()
                elif re.match(r'\w+\s\d{4}', date_str):  # Month Year
                    parsed_date = datetime.strptime(date_str, '%B %Y').date()
                else:
                    continue
                return str(parsed_date)
            except ValueError:
                continue  # If parsing fails, try next pattern
    return None

# Read jobs.json
with open('jobs.json', 'r') as f:
    data = json.load(f)

# Process each job
updated_count = 0
for job in data['jobs']:
    start_date = extract_start(job.get('description', ''))
    if start_date:
        job['start'] = start_date
        updated_count += 1
    else:
        job['start'] = ''

# Write back to jobs.json
with open('jobs.json', 'w') as f:
    json.dump(data, f, indent=2)

print(f"Updated {updated_count} jobs with extracted start dates.")
