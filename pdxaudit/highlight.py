"""Syntax colours for pdx-script in the --display app.

The rules and colours are those of the Paradox Highlight extension for VS Code
(dragon-archer.paradox-highlight, MIT licence, see syntax/LICENSE.txt): its EU5
TextMate grammar and its Paradox Dark theme, copied into syntax/. This module
is a small TextMate interpreter covering what that grammar uses: match and
begin/end rules, captures, includes, and left-priority injections."""
import json
import re
from functools import lru_cache
from pathlib import Path

from . import worker

SYNTAX = Path(__file__).with_name("syntax")
GRAMMARS = ("paradox.tmLanguage.json", "eu5.tmLanguage.json")
ROOT_SCOPE = "source.paradox.eu5"
THEME_CHAIN = ("dark_vs.json", "dark_plus.json", "dark_modern.json", "paradox-dark.json")
# Corrections to the copied grammars: {(scope name, repository key): {field: value}}.
# non-comparable-rhs ends a value only at whitespace, so in `a = { b = 1}` the value
# takes the `}` and the block stays open. Each such line then adds one level to the
# state that the next line starts from. The value now also ends before a `}`.
GRAMMAR_FIXES = {("source.paradox", "non-comparable-rhs"): {"end": r"\s+|(?=\})"}}

_LOOKBEHIND = re.compile(r"\(\?(<=|<!)((?:[^()\\]|\\.)*)\)")


def _python_regex(src):
    """Compile a grammar regex. Python needs fixed-width lookbehinds, so an
    alternation inside one becomes one lookbehind per alternative."""
    def split(m):
        kind, alts = m.group(1), re.split(r"(?<!\\)\|", m.group(2))
        if len(alts) == 1:
            return m.group(0)
        parts = [f"(?{kind}{a})" for a in alts]
        return "(?:" + "|".join(parts) + ")" if kind == "<=" else "".join(parts)
    return re.compile(_LOOKBEHIND.sub(split, src))


def _jsonc(path):
    """JSON with // and /* */ comments and trailing commas, as VS Code themes are."""
    text, out, i, in_str = path.read_text(encoding="utf-8"), [], 0, False
    while i < len(text):
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\":
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
            continue
        else:
            out.append(ch)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def _selects(selector, scope):
    return scope == selector or scope.startswith(selector + ".")


class _Rule:
    def __init__(self, raw, grammar, owner):
        self.name = tuple((raw.get("name") or "").split())
        self.content = tuple((raw.get("contentName") or "").split())
        self.match = _python_regex(raw["match"]) if "match" in raw else None
        self.begin = _python_regex(raw["begin"]) if "begin" in raw else None
        self.end = _python_regex(raw["end"]) if "end" in raw else None
        self.captures = raw.get("captures")
        self.begin_captures = raw.get("beginCaptures") or raw.get("captures")
        self.end_captures = raw.get("endCaptures") or raw.get("captures")
        self._raw, self._grammar, self._owner, self._patterns = raw, grammar, owner, None

    def patterns(self):
        if self._patterns is None:
            self._patterns = self._owner.flatten(self._raw.get("patterns"), self._grammar)
        return self._patterns


class _Group:
    """The patterns of a capture, scanned over just the captured text."""

    end = None

    def __init__(self, patterns):
        self._patterns = patterns

    def patterns(self):
        return self._patterns


class _Theme:
    def __init__(self):
        self.rules = []
        for name in THEME_CHAIN:
            for tc in _jsonc(SYNTAX / name).get("tokenColors", []):
                scopes = tc.get("scope") or []
                if isinstance(scopes, str):
                    scopes = scopes.split(",")
                for selector in scopes:
                    parts = selector.strip().split()
                    if parts:
                        self.rules.append((parts, len(self.rules), tc.get("settings") or {}))
        self._cache = {}

    def style(self, scopes):
        """(foreground or None, bold, italic) for a token's scope stack. Each scope,
        outermost first, applies its best-matching rule, so inner scopes win."""
        if scopes in self._cache:
            return self._cache[scopes]
        colour, bold, italic = None, False, False
        for depth, scope in enumerate(scopes):
            best = None
            for parts, index, settings in self.rules:
                if not _selects(parts[-1], scope) or not self._ancestors(parts[:-1], scopes[:depth]):
                    continue
                rank = (parts[-1].count(".") + 1, len(parts), index)
                if best is None or rank > best[0]:
                    best = (rank, settings)
            if best:
                settings = best[1]
                colour = settings.get("foreground", colour)
                if "fontStyle" in settings:
                    bold, italic = "bold" in settings["fontStyle"], "italic" in settings["fontStyle"]
        self._cache[scopes] = (colour, bold, italic)
        return self._cache[scopes]

    @staticmethod
    def _ancestors(parts, outer):
        i = 0
        for scope in outer:
            if i < len(parts) and _selects(parts[i], scope):
                i += 1
        return i == len(parts)


class Highlighter:
    def __init__(self):
        self.grammars = {}
        for name in GRAMMARS:
            raw = json.loads((SYNTAX / name).read_text(encoding="utf-8"))
            for (scope, key), fields in GRAMMAR_FIXES.items():
                if scope == raw["scopeName"]:
                    raw["repository"][key].update(fields)
            self.grammars[raw["scopeName"]] = raw
        self._rules = {}
        self.injections = []
        for scope_name, grammar in self.grammars.items():
            for selector, injection in (grammar.get("injections") or {}).items():
                targets = [s.strip()[2:] if s.strip().startswith("L:") else s.strip()
                           for s in selector.split(",")]
                self.injections.append((targets, injection, scope_name))
        self._injected = {}
        self.root = (None, (ROOT_SCOPE,), (ROOT_SCOPE,))
        self.root_patterns = self.flatten(self.grammars[ROOT_SCOPE]["patterns"], ROOT_SCOPE)
        self.theme = _Theme()

    def _rule(self, raw, grammar):
        key = id(raw)
        if key not in self._rules:
            self._rules[key] = _Rule(raw, grammar, self)
        return self._rules[key]

    def flatten(self, entries, grammar, seen=frozenset()):
        """The match and begin rules a pattern list stands for, with includes and
        pattern-only groups expanded in place."""
        out = []
        for entry in entries or []:
            if "include" in entry:
                ref = entry["include"]
                target, _, key = (grammar, "#", ref[1:]) if ref.startswith("#") else ref.partition("#")
                marker = (target, key)
                g = self.grammars.get(target)
                if marker in seen or g is None:
                    continue
                node = g.get("repository", {}).get(key) if key else {"patterns": g["patterns"]}
                if node is None:
                    continue
                if "match" in node or "begin" in node:
                    out.append(self._rule(node, target))
                else:
                    out.extend(self.flatten(node.get("patterns"), target, seen | {marker}))
            elif "match" in entry or "begin" in entry:
                out.append(self._rule(entry, grammar))
            else:
                out.extend(self.flatten(entry.get("patterns"), grammar, seen))
        return out

    def _injections_for(self, scopes):
        if scopes not in self._injected:
            rules = []
            for targets, injection, grammar in self.injections:
                if any(_selects(t, s) for t in targets for s in scopes):
                    rules.extend(self.flatten(injection.get("patterns"), grammar))
            self._injected[scopes] = rules
        return self._injected[scopes]

    def _emit(self, tokens, text, m, base, captures, grammar):
        start, end = m.span()
        if end <= start:
            return
        scopes = [base] * (end - start)
        nested = []
        for group, cap in sorted((captures or {}).items(), key=lambda kv: int(kv[0])):
            group = int(group)
            if group > m.re.groups:
                continue
            s, e = m.span(group)
            if s < 0 or e <= s:
                continue
            extra = tuple((cap.get("name") or "").split())
            for i in range(s, e):
                scopes[i - start] = scopes[i - start] + extra
            if cap.get("patterns"):
                nested.append((s, e, base + extra, cap))
        for s, e, cap_base, cap in nested:
            key = id(cap)
            if key not in self._rules:
                self._rules[key] = _Group(self.flatten(cap["patterns"], grammar))
            inside = []
            self._scan(text, s, e, [(self._rules[key], cap_base, cap_base)], inside)
            for a, b, sc in inside:
                for i in range(a, b):
                    scopes[i - start] = sc
        run = start
        for i in range(start + 1, end + 1):
            if i == end or scopes[i - start] != scopes[run - start]:
                tokens.append((run, i, scopes[run - start]))
                run = i

    def _scan(self, text, pos, limit, stack, tokens):
        """Tokenize text[pos:limit] from `stack`, appending (start, end, scopes) to
        tokens; returns the stack left open at `limit`."""
        found, blocked = {}, set()
        budget = 40 * (limit - pos) + 200
        while pos < limit and budget > 0:
            budget -= 1
            rule, outer, inner = stack[-1]
            best, best_rule, is_end = None, None, False
            if rule is not None and rule.end is not None:
                best = rule.end.search(text, pos, limit)
                is_end = best is not None
            for candidate in self._injections_for(inner) + (rule.patterns() if rule else self.root_patterns):
                if candidate in blocked:
                    continue
                rx = candidate.match or candidate.begin
                # Keyed by id: a Pattern hashes its whole compiled code on each call,
                # and the grammar's keyword lists are long.
                m = found.get(id(rx), False)
                if m is False or (m is not None and m.start() < pos):
                    m = rx.search(text, pos, limit)
                    found[id(rx)] = m
                if m is not None and (best is None or m.start() < best.start()):
                    best, best_rule, is_end = m, candidate, False
            if best is None:
                break
            if best.start() > pos:
                tokens.append((pos, best.start(), inner))
            if is_end:
                self._emit(tokens, text, best, outer, rule.end_captures, rule._grammar)
                stack = stack[:-1]
                blocked.add(rule)
            elif best_rule.match is not None:
                self._emit(tokens, text, best, inner + best_rule.name, best_rule.captures, best_rule._grammar)
                if best.end() == best.start():
                    blocked.add(best_rule)
            else:
                opened = inner + best_rule.name
                self._emit(tokens, text, best, opened, best_rule.begin_captures, best_rule._grammar)
                stack = stack + [(best_rule, opened, opened + best_rule.content)]
            if best.end() > pos:
                pos = best.end()
                blocked = set()
        if pos < limit:
            tokens.append((pos, limit, stack[-1][2]))
        return stack

    def _line(self, line, stack):
        text = line + "\n"
        tokens = []
        stack = self._scan(text, 0, len(text), stack, tokens)
        return tokens, stack

    def line_spans(self, lines):
        """For each line, [(text, colour or None, bold, italic)], with the grammar's
        state carried from one line to the next."""
        stack, out = [self.root], []
        for line in lines:
            tokens, stack = self._line(line, stack)
            spans = []
            for start, end, scopes in tokens:
                end = min(end, len(line))
                if start >= end:
                    continue
                style = self.theme.style(scopes)
                if spans and spans[-1][1:] == style:
                    spans[-1] = (spans[-1][0] + line[start:end],) + style
                else:
                    spans.append((line[start:end],) + style)
            out.append(spans)
        return out


@lru_cache(maxsize=None)
def highlighter():
    return Highlighter()


def _spans_of(groups):
    hl = highlighter()
    return [hl.line_spans(lines) for lines in groups]


def spans_of(groups):
    """line_spans of each group of lines, one grammar state for each group, worked out
    in the app's colour worker process (worker.call). Call it from a worker thread."""
    if not any(groups):
        return [[] for _g in groups]
    return worker.call("colour", "pdxaudit.highlight:_spans_of", groups)
