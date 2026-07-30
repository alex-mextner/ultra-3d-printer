"""stdio MCP server that forwards tool calls to the ClaudeBridge add-in.

Claude Code speaks MCP over stdio to this process; this process speaks a small
private JSON protocol to http://127.0.0.1:9765, which is served by the
ClaudeBridge add-in running inside Fusion 360.

Deliberately stdlib-only: Fusion's bundled CPython has no pip.
"""

import json
import sys
import urllib.error
import urllib.request

BRIDGE = 'http://127.0.0.1:9765'
PROTOCOL_VERSION = '2025-06-18'

TOOLS = [
    {
        'name': 'fusion_ping',
        'description': 'Check that Fusion 360 is reachable and report the active '
                       'document, design type and display units.',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'fusion_get_selection',
        'description': 'Read whatever the user currently has selected in Fusion 360. '
                       'Returns entity type, owning body/component, bounding box and '
                       'area in mm, and for planar faces the plane origin and normal.',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'fusion_run_script',
        'description': 'Execute Python against the Fusion 360 API on Fusion\'s main '
                       'thread. Pre-bound names: adsk, app, ui, design, root, math, '
                       'and mm()/to_mm() converters. NOTE: the Fusion API works in '
                       'CENTIMETRES regardless of document display units. Assign to '
                       '`result` to return a value; stdout is captured.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'code': {'type': 'string', 'description': 'Python source to execute.'},
            },
            'required': ['code'],
            'additionalProperties': False,
        },
    },
]

_TOOL_TO_BRIDGE = {
    'fusion_ping': 'ping',
    'fusion_get_selection': 'get_selection',
    'fusion_run_script': 'run_script',
}


def _bridge_call(tool, args, timeout=180):
    payload = json.dumps({'tool': tool, 'args': args, 'timeout': timeout}).encode('utf-8')
    req = urllib.request.Request(BRIDGE + '/call', data=payload,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as exc:
        return {'ok': False,
                'error': 'cannot reach the ClaudeBridge add-in at %s (%s). Is Fusion 360 '
                         'running with the ClaudeBridge add-in started under '
                         'Utilities -> Add-Ins?' % (BRIDGE, exc)}
    except Exception as exc:  # noqa: BLE001 - surface anything to the model
        return {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)}


def _send(msg):
    sys.stdout.write(json.dumps(msg) + '\n')
    sys.stdout.flush()


def _result(req_id, payload):
    _send({'jsonrpc': '2.0', 'id': req_id, 'result': payload})


def _error(req_id, code, message):
    _send({'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}})


def _handle(msg):
    method = msg.get('method')
    req_id = msg.get('id')

    # notifications carry no id and must not be answered
    if req_id is None:
        return

    if method == 'initialize':
        client_version = (msg.get('params') or {}).get('protocolVersion')
        _result(req_id, {
            'protocolVersion': client_version or PROTOCOL_VERSION,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'fusion360-bridge', 'version': '1.0.0'},
        })
    elif method == 'ping':
        _result(req_id, {})
    elif method == 'tools/list':
        _result(req_id, {'tools': TOOLS})
    elif method == 'tools/call':
        params = msg.get('params') or {}
        name = params.get('name')
        bridge_tool = _TOOL_TO_BRIDGE.get(name)
        if bridge_tool is None:
            _error(req_id, -32602, 'unknown tool: %s' % name)
            return
        response = _bridge_call(bridge_tool, params.get('arguments') or {})
        _result(req_id, {
            'content': [{'type': 'text', 'text': json.dumps(response, indent=2, ensure_ascii=False)}],
            'isError': not response.get('ok', False),
        })
    else:
        _error(req_id, -32601, 'method not found: %s' % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(msg)
        except Exception as exc:  # noqa: BLE001 - never die on one bad message
            if isinstance(msg, dict) and msg.get('id') is not None:
                _error(msg['id'], -32603, '%s: %s' % (type(exc).__name__, exc))


if __name__ == '__main__':
    main()
