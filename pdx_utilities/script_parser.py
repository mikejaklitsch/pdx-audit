"""Tokenizer and parser for PDX script and GUI files, shared by the tools.

tokenize(text) turns text into token dicts; parse(tokens, text) turns those into
a tree of node dicts. Each node has:
  - type: 'node', 'comment', or 'raw_block'
  - key: the keyword/identifier (for 'node' type)
  - op: operator like '=', '>', '<' etc. (or None)
  - val: scalar value, 'PENDING_BLOCK' during parsing, or list of child nodes
  - Optional metadata: _blank_before, _cm_preceding, _cm_inline, _cm_open, _cm_close
  - With parse(..., positions=True), character offsets into the parsed text:
    _start (where the node begins), _end (the end of its value, or of a block's
    closing brace) and, for a block, _open_end (the end of its '{'). They are left
    out by default: a formatter sizes blocks from the node dicts themselves

parse() is strict by default: a stray '}' raises MisnestedBracesError, so a
formatter can refuse the file instead of losing text. parse(..., strict=False)
reads files as the game does, skipping a stray '}' and closing blocks left open
at the end of the text.
"""
import re

RAW_BLOCKS = ('in_breach_of', 'inverted_switch')

TOKEN_PATTERN = re.compile(
    r'(#.*)'            # comment
    r'|("[^"]*")'       # quoted string
    r'|(@\\?\[[^\]]+\])'  # @[] variable reference
    r'|(\[\[!?[^\]]*\])'  # [[]] scripted condition
    r'|(\?=|!=|>=|<=|[=\{\}<>!?])'  # operators
    r'|([^\s=\{\}<>!?]+)'  # word
    r'|\n'              # newline (for line counting)
)


def format_comment(val):
    """Normalize comment spacing: ensure space after # (except ## headers)."""
    if not val.startswith('##'):
        if len(val) > 1 and not val[1].isspace():
            return f"# {val[1:]}"
    return val


def tokenize(text):
    """Convert raw text into a list of token dicts.

    Each token has: type, val, line, pre (gap text), start, end.
    Tokens preceded by blank lines get a '_blank_before' flag.
    """
    tokens = []
    current_line = 1
    last_idx = 0

    for match in TOKEN_PATTERN.finditer(text):
        start, end = match.span()
        val = match.group(0)
        gap = text[last_idx:start]
        last_idx = end

        if val == '\n':
            current_line += 1
            continue

        if match.group(1):
            t_type = 'comment'
            val = format_comment(match.group(1))
        elif match.group(2):
            t_type = 'str'
            val = match.group(2)
            current_line += val.count('\n')  # strings can span lines
        elif match.group(3):
            t_type = 'word'
            val = match.group(3)
            current_line += val.count('\n')
        elif match.group(4):
            t_type = 'word'
            val = match.group(4)
            current_line += val.count('\n')
        elif match.group(5):
            t_type = 'op'
            val = match.group(5)
        elif match.group(6):
            t_type = 'word'
            val = match.group(6)
        else:
            continue

        tokens.append({
            'type': t_type, 'val': val, 'line': current_line,
            'pre': gap, 'start': start, 'end': end,
        })

    # Mark tokens preceded by blank lines (line gap >= 2)
    for idx in range(1, len(tokens)):
        if tokens[idx]['line'] - tokens[idx - 1]['line'] >= 2:
            tokens[idx]['_blank_before'] = True

    return tokens


class MisnestedBracesError(ValueError):
    """Braces are mis-nested (e.g. a stray '}' before its '{') even though
    total counts may match. Formatting must refuse rather than truncate."""

    def __init__(self, line):
        self.line = line
        super().__init__(f"stray '}}' at line {line} closes nothing")


def _get_inline_comment(tokens, current_idx, current_line):
    """Check if the next token is an inline comment on the same line.

    Returns (comment_text, offset) where offset is 1 if found, 0 if not.
    """
    if current_idx + 1 < len(tokens):
        next_t = tokens[current_idx + 1]
        if next_t['type'] == 'comment' and next_t['line'] == current_line:
            return next_t['pre'] + next_t['val'], 1
    return None, 0


def _attach_metadata(node, token, preceding_comments):
    """Attach preceding comments and blank-before flag to a node."""
    if preceding_comments:
        node['_cm_preceding'] = [c['val'] for c in preceding_comments]
    if token.get('_blank_before'):
        node['_blank_before'] = True


def _skip_comments(tokens, start_idx):
    """Advance past comment tokens, returning the next non-comment index."""
    idx = start_idx
    while idx < len(tokens) and tokens[idx]['type'] == 'comment':
        idx += 1
    return idx


def _parse_raw_block(tokens, text, token, i, raw_blocks):
    """Try to parse a raw block (switch, inverted_switch). Returns (node, next_i) or None."""
    token_val = tokens[i]['val']
    if tokens[i]['type'] != 'word' or token_val not in raw_blocks:
        return None

    op_idx = _skip_comments(tokens, i + 1)
    if op_idx >= len(tokens) or tokens[op_idx]['val'] != '=':
        return None

    brace_idx = _skip_comments(tokens, op_idx + 1)
    if brace_idx >= len(tokens) or tokens[brace_idx]['val'] != '{':
        return None

    # Scan for matching close brace
    brace_level = 1
    scan_idx = brace_idx + 1
    while scan_idx < len(tokens):
        if tokens[scan_idx]['val'] == '{':
            brace_level += 1
        elif tokens[scan_idx]['val'] == '}':
            brace_level -= 1
            if brace_level == 0:
                raw_text = text[token['start']:tokens[scan_idx]['end']]
                node = {'type': 'raw_block', 'val': raw_text,
                        '_start': token['start'], '_end': tokens[scan_idx]['end']}
                if token.get('_blank_before'):
                    node['_blank_before'] = True
                return node, scan_idx + 1
        scan_idx += 1

    return None


def _parse_block_weight_pattern(tokens, text, token, i):
    """Try to parse { script_value } = { effects } pattern (random_list weights).

    Returns (node, next_i) or None.
    """
    if token['val'] != '{':
        return None

    # Find matching close brace for the weight block
    brace_level = 1
    scan_idx = i + 1
    while scan_idx < len(tokens):
        if tokens[scan_idx]['val'] == '{':
            brace_level += 1
        elif tokens[scan_idx]['val'] == '}':
            brace_level -= 1
            if brace_level == 0:
                break
        scan_idx += 1
    else:
        return None

    # Check if next non-comment token after } is =
    eq_idx = _skip_comments(tokens, scan_idx + 1)
    if eq_idx >= len(tokens) or tokens[eq_idx]['val'] != '=':
        return None

    # Find the block after =
    block_idx = _skip_comments(tokens, eq_idx + 1)
    if block_idx >= len(tokens) or tokens[block_idx]['val'] != '{':
        return None

    # Find matching close brace for the effects block
    brace_level = 1
    end_idx = block_idx + 1
    while end_idx < len(tokens):
        if tokens[end_idx]['val'] == '{':
            brace_level += 1
        elif tokens[end_idx]['val'] == '}':
            brace_level -= 1
            if brace_level == 0:
                raw_text = text[token['start']:tokens[end_idx]['end']]
                node = {'type': 'raw_block', 'val': raw_text,
                        '_start': token['start'], '_end': tokens[end_idx]['end']}
                if token.get('_blank_before'):
                    node['_blank_before'] = True
                return node, end_idx + 1
        end_idx += 1

    return None


def _collect_lookahead(tokens, start_idx, count=5):
    """Collect up to `count` non-comment tokens starting at start_idx."""
    lookahead = []
    idx = start_idx
    while idx < len(tokens) and len(lookahead) < count:
        if tokens[idx]['type'] != 'comment':
            lookahead.append((idx, tokens[idx]))
        idx += 1
    return lookahead


def _parse_node_pattern(tokens, token, i, lookahead):
    """Determine the node pattern from lookahead tokens.

    Returns (node_dict, skip_to_index) or (None, None).
    Patterns handled:
      word {                  -> key { block }
      word = value            -> key = value
      word = {                -> key = { block }
      word = value {          -> key = val_key { block }
      word word {             -> key val_key { block }
      word "str" {            -> key val_key { block }
      word word = word {      -> key mid_key = val_key { block }
    """
    token_val = token['val']
    if not lookahead:
        return None, None

    t1_idx, t1 = lookahead[0]

    # word {
    if t1['val'] == '{':
        node = {'key': token_val, 'op': None, 'val': 'PENDING_BLOCK', 'type': 'node',
                '_token_start': token['start'], '_start': token['start']}
        return node, t1_idx

    # word = ...
    if t1['type'] == 'op' and t1['val'] not in ['{', '}']:
        operator = t1['val']
        if len(lookahead) >= 2:
            t2_idx, t2 = lookahead[1]
            # word = {
            if t2['val'] == '{':
                node = {'key': token_val, 'op': operator, 'val': 'PENDING_BLOCK', 'type': 'node',
                        '_token_start': token['start'], '_start': token['start']}
                return node, t2_idx
            # word = value ...
            if t2['type'] in ('word', 'str'):
                if len(lookahead) >= 3:
                    t3_idx, t3 = lookahead[2]
                    # word = value {
                    if t3['val'] == '{':
                        node = {'key': token_val, 'op': operator, 'val_key': t2['val'],
                                'val': 'PENDING_BLOCK', 'type': 'node', '_token_start': token['start'],
                                '_start': token['start']}
                        return node, t3_idx
                # word = value (no block)
                node = {'key': token_val, 'op': operator, 'val': t2['val'], 'type': 'node',
                        '_start': token['start'], '_end': t2['end']}
                cm, offset = _get_inline_comment(tokens, t2_idx, t2['line'])
                if cm:
                    node['_cm_inline'] = cm
                    t2_idx += offset
                return node, t2_idx + 1
        return None, None

    # word word ... or word "str" ...
    if t1['type'] in ('word', 'str'):
        mid_val = t1['val']
        if len(lookahead) >= 2:
            t2_idx, t2 = lookahead[1]
            # word word {
            if t2['val'] == '{':
                node = {'key': token_val, 'op': None, 'val_key': mid_val,
                        'val': 'PENDING_BLOCK', 'type': 'node', '_token_start': token['start'],
                        '_start': token['start']}
                return node, t2_idx
            # word word = ...
            if t2['type'] == 'op' and t2['val'] not in ['{', '}']:
                operator = t2['val']
                if len(lookahead) >= 3:
                    t3_idx, t3 = lookahead[2]
                    if t3['type'] in ('word', 'str'):
                        if len(lookahead) >= 4:
                            t4_idx, t4 = lookahead[3]
                            # word word = value {
                            if t4['val'] == '{':
                                node = {'key': token_val, 'op': operator, 'mid_key': mid_val,
                                        'val_key': t3['val'], 'val': 'PENDING_BLOCK', 'type': 'node',
                                        '_token_start': token['start'], '_start': token['start']}
                                return node, t4_idx
                        # word word = value (no block) - treat as word word { pending
                        node = {'key': token_val, 'op': None, 'val_key': mid_val,
                                'val': 'PENDING_BLOCK', 'type': 'node', '_token_start': token['start'],
                                '_start': token['start']}
                        return node, t2_idx

    return None, None


def _strip_positions(nodes):
    for node in nodes:
        for field in ('_start', '_end', '_open_end'):
            node.pop(field, None)
        if isinstance(node.get('val'), list):
            _strip_positions(node['val'])


def parse(tokens, text, raw_blocks=RAW_BLOCKS, strict=True, positions=False, keyless=False):
    """Parse a token list into an AST (nested list of node dicts).

    strict=False skips a stray '}' and closes blocks still open at the end of the
    text, instead of raising or returning only the innermost open block.
    positions=True keeps each node's _start, _end and _open_end offsets; without
    it the nodes are exactly as a formatter has always received them.
    keyless=True keeps a block that has no key (`{ 0.0 1.0 }` in a list) as a node
    with key None; without it such a block's contents are left out, as a formatter
    has always received them."""
    stack = []
    opens = []          # (start, end) of each open '{', innermost last
    current_list = []
    i = 0
    preceding_comments = []

    while i < len(tokens):
        token = tokens[i]
        token_line = token['line']
        token_val = token['val']

        # Handle raw blocks (switch, inverted_switch)
        result = _parse_raw_block(tokens, text, token, i, raw_blocks)
        if result:
            node, next_i = result
            preceding_comments = []
            current_list.append(node)
            i = next_i
            continue

        # Comments
        if token['type'] == 'comment':
            current_list.append(token)
            preceding_comments.append(token)
            i += 1
            continue

        # Close brace
        if token_val == '}':
            if not stack:
                if not strict:
                    i += 1
                    continue
                # A silent break here would drop the rest of the file
                raise MisnestedBracesError(token_line)
            finished_list = current_list
            current_list = stack.pop()
            open_start, open_end = opens.pop()
            if current_list and current_list[-1].get('val') == 'PENDING_BLOCK':
                parent_node = current_list[-1]
                parent_node['val'] = finished_list
                parent_node['_end'] = token['end']
                if parent_node.get('key') == 'switch' and '_token_start' in parent_node:
                    parent_node['_raw'] = text[parent_node['_token_start']:token['end']]
                cm, offset = _get_inline_comment(tokens, i, token_line)
                if cm:
                    parent_node['_cm_close'] = cm
                    i += offset
            elif keyless:
                current_list.append({'key': None, 'op': None, 'val': finished_list, 'type': 'node',
                                     '_start': open_start, '_open_end': open_end,
                                     '_end': token['end']})
            preceding_comments = []
            i += 1
            continue

        # { script_value } = { effects } pattern (random_list weights)
        if token_val == '{':
            result = _parse_block_weight_pattern(tokens, text, token, i)
            if result:
                node, next_i = result
                _attach_metadata(node, token, preceding_comments)
                preceding_comments = []
                current_list.append(node)
                i = next_i
                continue

        # Open brace (bare block without key)
        if token_val == '{':
            if current_list and current_list[-1].get('val') == 'PENDING_BLOCK':
                current_list[-1]['_open_end'] = token['end']
                cm, offset = _get_inline_comment(tokens, i, token_line)
                if cm:
                    current_list[-1]['_cm_open'] = cm
                    i += offset
            stack.append(current_list)
            opens.append((token['start'], token['end']))
            current_list = []
            i += 1
            continue

        # Word/string token - determine pattern via lookahead
        lookahead = _collect_lookahead(tokens, i + 1)
        node, skip_to = _parse_node_pattern(tokens, token, i, lookahead)

        if node:
            _attach_metadata(node, token, preceding_comments)
            preceding_comments = []
            current_list.append(node)
            i = skip_to
            continue

        # Fallback: standalone word
        node = {'key': token_val, 'val': None, 'type': 'node',
                '_start': token['start'], '_end': token['end']}
        _attach_metadata(node, token, preceding_comments)
        preceding_comments = []
        cm, offset = _get_inline_comment(tokens, i, token_line)
        if cm:
            node['_cm_inline'] = cm
            i += offset
        current_list.append(node)
        i += 1

    if not strict:
        # Blocks left open at the end of the text close there, keeping every
        # enclosing level's content.
        while stack:
            finished_list = current_list
            current_list = stack.pop()
            if current_list and current_list[-1].get('val') == 'PENDING_BLOCK':
                current_list[-1]['val'] = finished_list
                current_list[-1]['_end'] = len(text)
            else:
                current_list.extend(finished_list)

    if not positions:
        _strip_positions(current_list)
    return current_list
