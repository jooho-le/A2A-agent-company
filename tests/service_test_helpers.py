"""Explicit service designs for offline fixtures, not model or product results."""
def service_design(delivery='API', path='/items', fields=None):
    return {'delivery': delivery, 'storage': 'SQLITE', 'dataModel': 'User: id, email, password_hash' if path == '/signup' else 'Item: id, title, done',
            'implementationDecisions': ['Use the approved local runtime and JSON API.'],
            'endpoints': [
                {'method': 'GET', 'path': '/', 'bodyFormat': 'NONE', 'requestFields': [],
                 'responseFormat': 'HTML' if delivery == 'WEB' else 'JSON', 'successStatuses': [200], 'rejectionStatuses': []},
                {'method': 'POST', 'path': path, 'bodyFormat': 'JSON', 'requestFields': ['title'] if fields is None else fields,
                 'responseFormat': 'JSON', 'successStatuses': [200, 201], 'rejectionStatuses': [400, 409, 422]},
            ]}
