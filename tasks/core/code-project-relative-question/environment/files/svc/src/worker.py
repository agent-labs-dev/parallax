MAX_ATTEMPTS = 3

def handle_retry(job, attempts):
    if attempts >= MAX_ATTEMPTS:
        return 'dead-letter'
    return f'retry:{job}'
