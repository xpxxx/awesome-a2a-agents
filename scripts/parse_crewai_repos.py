#!/usr/bin/env python3
"""
Repository parser for CrewAI agents and tools.

Scans all Git repos under a root (default: ./crewai-repos) and extracts:
  - Agents defined via crewai.Agent(...)
  - Tools used by each agent, resolving developer-defined tools (in-repo)
    and marking crewai_tools classes as external.

Notes:
  - Python-only AST parsing. No code execution.
  - Resilient to syntax errors per file.
  - Ignores venv/.venv/node_modules/.git/__pycache__/build artifacts.
  - Produces one JSON document per the prompt's schema.
"""

import argparse
import ast
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


IGNORED_DIR_NAMES = {
    '.git', '__pycache__', 'node_modules', 'venv', '.venv', 'build', 'dist', '.mypy_cache', '.pytest_cache'
}

DEFAULT_SCAN_ROOT = Path('crewai-repos')


@dataclass
class DefinitionInfo:
    name: str
    kind: str  # 'class' | 'function'
    module: str
    file_path: Path
    doc_first_line: Optional[str]


@dataclass
class WarningInfo:
    file: str
    reason: str


def iter_python_files(root: Path) -> List[Path]:
    py_files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored directories in-place
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIR_NAMES]
        for filename in filenames:
            if filename.endswith('.py') and not filename.startswith('.'):  # skip dotfiles
                py_files.append(Path(dirpath) / filename)
    return py_files


def module_name_for_file(repo_root: Path, file_path: Path) -> str:
    rel = file_path.relative_to(repo_root)
    # Drop .py and convert path to dotted module
    return '.'.join(list(rel.with_suffix('').parts))


def first_line_or_none(doc: Optional[str]) -> Optional[str]:
    if not doc:
        return None
    first = doc.strip().splitlines()
    return first[0].strip() if first else None


def build_repo_symbol_index(repo_root: Path) -> Tuple[Dict[str, DefinitionInfo], Dict[Path, ast.AST], List[WarningInfo]]:
    """Build index of top-level class/function definitions keyed by fully qualified qualname.

    Returns: (qualname_to_def, file_to_ast, warnings)
    """
    qual_to_def: Dict[str, DefinitionInfo] = {}
    file_to_ast: Dict[Path, ast.AST] = {}
    warnings: List[WarningInfo] = []

    for py in iter_python_files(repo_root):
        try:
            with py.open('r', encoding='utf-8', errors='replace') as f:
                src = f.read()
            tree = ast.parse(src)
            file_to_ast[py] = tree
        except SyntaxError as e:
            warnings.append(WarningInfo(file=str(py), reason=f'SyntaxError: {e.msg}'))
            continue
        except Exception as e:  # unforeseen read/parse errors
            warnings.append(WarningInfo(file=str(py), reason=f'ParseError: {e}'))
            continue

        module = module_name_for_file(repo_root, py)

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                qual = f"{module}.{node.name}"
                qual_to_def[qual] = DefinitionInfo(
                    name=node.name,
                    kind='class',
                    module=module,
                    file_path=py,
                    doc_first_line=first_line_or_none(ast.get_docstring(node)),
                )
            elif isinstance(node, ast.FunctionDef):
                qual = f"{module}.{node.name}"
                qual_to_def[qual] = DefinitionInfo(
                    name=node.name,
                    kind='function',
                    module=module,
                    file_path=py,
                    doc_first_line=first_line_or_none(ast.get_docstring(node)),
                )

    return qual_to_def, file_to_ast, warnings


def collect_import_aliases(tree: ast.AST) -> Dict[str, str]:
    """Map of alias name -> imported fully-qualified name or module.

    Examples:
      from crewai import Agent as A   => { 'A': 'crewai.Agent' }
      import crewai as c              => { 'c': 'crewai' }
      from mypkg.tools import X       => { 'X': 'mypkg.tools.X' }
    """
    aliases: Dict[str, str] = {}
    for node in getattr(tree, 'body', []):
        if isinstance(node, ast.Import):
            for alias in node.names:
                asname = alias.asname or alias.name
                aliases[asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ''
            for alias in node.names:
                asname = alias.asname or alias.name
                if alias.name == '*':
                    # Not supported; skip star imports
                    continue
                aliases[asname] = f"{mod}.{alias.name}" if mod else alias.name
    return aliases


def is_agent_constructor(func: ast.AST, import_aliases: Dict[str, str]) -> bool:
    # Name("Agent") or Name(alias) where alias resolves to crewai.Agent
    if isinstance(func, ast.Name):
        target = import_aliases.get(func.id)
        if target == 'crewai.Agent':
            return True
        if func.id == 'Agent':
            # Could be direct from crewai import Agent without alias resolution
            return True
        return False
    # crewai.Agent or alias.Agent
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        mod = import_aliases.get(func.value.id, func.value.id)
        full = f"{mod}.{func.attr}"
        return full == 'crewai.Agent'
    return False


def extract_str_or_snippet(node: Optional[ast.AST], src: str) -> Optional[str]:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # Fallback to raw source slice
    try:
        return src[node.col_offset: node.end_col_offset] if hasattr(node, 'col_offset') and hasattr(node, 'end_col_offset') else None
    except Exception:
        return None


def guess_llm_label(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.lower()
    if any(k in v for k in ['gpt', 'o1', 'o3']):
        return f"openai:{value}"
    if 'claude' in v:
        return f"anthropic:{value}"
    if 'gemini' in v:
        return f"google:{value}"
    if 'llama' in v or 'ollama' in v:
        return f"ollama:{value}"
    if 'mistral' in v:
        return f"mistral:{value}"
    return 'unknown'


def find_arg(call: ast.Call, names: List[str]) -> Optional[ast.AST]:
    # keyword first
    for kw in call.keywords:
        if kw.arg in names:
            return kw.value
    # positional heuristic by common order: name, role, goal, tools/llm vary widely; skip positional
    return None


def resolve_symbol_in_repo(symbol: str, import_aliases: Dict[str, str], current_module: str, qual_index: Dict[str, DefinitionInfo]) -> Optional[DefinitionInfo]:
    # Try current module first
    cand = f"{current_module}.{symbol}"
    if cand in qual_index:
        return qual_index[cand]

    # Try imported aliases
    target = import_aliases.get(symbol)
    if target:
        # If it names a symbol (module.symbol)
        if '.' in target:
            # Might be module path ending with symbol
            if target in qual_index:
                return qual_index[target]
            # Or module path + symbol implicit
            # Already full; nothing else to try
        else:
            # It is a module alias; cannot resolve symbol from here without attribute context
            pass

    # Try any module in index that ends with .symbol (unique heuristic)
    suffix = f".{symbol}"
    matches = [d for q, d in qual_index.items() if q.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return None


def extract_tools_from_value(value: ast.AST, import_aliases: Dict[str, str], current_module: str, qual_index: Dict[str, DefinitionInfo]) -> List[dict]:
    tools: List[dict] = []

    def record_external(name: str) -> None:
        tools.append({
            'name': name,
            'qualname': None,
            'kind': 'class',
            'defined_in': 'external',
            'docstring': None,
        })

    def record_unresolved(name: str) -> None:
        tools.append({
            'name': name,
            'qualname': None,
            'kind': 'unknown',
            'defined_in': 'unknown',
            'docstring': None,
        })

    def record_def(defn: DefinitionInfo) -> None:
        tools.append({
            'name': defn.name,
            'qualname': f"{defn.module}.{defn.name}",
            'kind': defn.kind,
            'defined_in': str(defn.file_path),
            'docstring': defn.doc_first_line,
        })

    def handle_symbol(name: str) -> None:
        # crewai_tools import detection
        target = import_aliases.get(name)
        if target and (target.startswith('crewai_tools') or target == 'crewai_tools'):
            record_external(name)
            return

        defn = resolve_symbol_in_repo(name, import_aliases, current_module, qual_index)
        if defn:
            record_def(defn)
        else:
            record_unresolved(name)

    # Normalize list-like containers
    nodes: List[ast.AST] = []
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        nodes = list(value.elts)
    else:
        nodes = [value]

    for node in nodes:
        if isinstance(node, ast.Call):
            # Tool instantiated: FooTool(...)
            fn = node.func
            if isinstance(fn, ast.Name):
                handle_symbol(fn.id)
            elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                # module_alias.ClassName(...) -> try resolving ClassName; if module alias is crewai_tools, external
                mod_alias = import_aliases.get(fn.value.id, fn.value.id)
                if mod_alias and mod_alias.startswith('crewai_tools'):
                    record_external(fn.attr)
                else:
                    handle_symbol(fn.attr)
            else:
                # complex call expression
                tools.append({
                    'name': getattr(getattr(node.func, 'id', None), 'id', None) or 'unknown',
                    'qualname': None,
                    'kind': 'unknown',
                    'defined_in': 'unknown',
                    'docstring': None,
                })
        elif isinstance(node, ast.Name):
            # Bare function ref or class ref
            handle_symbol(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            # module_alias.symbol
            mod_alias = import_aliases.get(node.value.id, node.value.id)
            if mod_alias and mod_alias.startswith('crewai_tools'):
                record_external(node.attr)
            else:
                handle_symbol(node.attr)
        else:
            # Unknown expression
            tools.append({
                'name': 'unknown',
                'qualname': None,
                'kind': 'unknown',
                'defined_in': 'unknown',
                'docstring': None,
            })

    return tools


def parse_repo(repo_root: Path) -> Tuple[List[dict], List[WarningInfo], int]:
    """Parse a single repo; returns (agents, warnings, files_parsed_count)."""
    qual_index, file_to_ast, warnings = build_repo_symbol_index(repo_root)
    agents: List[dict] = []
    files_parsed = 0

    for file_path, tree in file_to_ast.items():
        files_parsed += 1
        try:
            with file_path.open('r', encoding='utf-8', errors='replace') as f:
                src = f.read()
        except Exception as e:
            warnings.append(WarningInfo(file=str(file_path), reason=f'ReadError: {e}'))
            continue

        import_aliases = collect_import_aliases(tree)
        current_module = module_name_for_file(repo_root, file_path)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and is_agent_constructor(node.func, import_aliases):
                name = None
                # Best-effort: find assignment target name if present
                parent_assign = None
                for parent in getattr(tree, 'body', []):
                    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and getattr(parent, 'value', None) is node:
                        parent_assign = parent
                        break
                if isinstance(parent_assign, ast.Assign) and parent_assign.targets:
                    t0 = parent_assign.targets[0]
                    if isinstance(t0, ast.Name):
                        name = t0.id
                elif isinstance(parent_assign, ast.AnnAssign) and isinstance(parent_assign.target, ast.Name):
                    name = parent_assign.target.id
                if name is None:
                    # fallback generic name
                    name = 'agent'

                role_node = find_arg(node, ['role'])
                goal_node = find_arg(node, ['goal'])
                llm_node = find_arg(node, ['llm', 'model'])
                tools_node = find_arg(node, ['tools', 'tool', 'toolkit'])

                role_val = extract_str_or_snippet(role_node, src)
                goal_val = extract_str_or_snippet(goal_node, src)
                llm_val_raw = extract_str_or_snippet(llm_node, src)
                llm_label = guess_llm_label(llm_val_raw)

                tools_used: List[dict] = []
                if tools_node is not None:
                    tools_used = extract_tools_from_value(tools_node, import_aliases, current_module, qual_index)

                agents.append({
                    'name': name,
                    'role': role_val,
                    'goal': goal_val,
                    'llm': llm_label,
                    'tools_used': tools_used,
                })

    return agents, warnings, files_parsed


def parse_all(scan_root: Path, overall_timeout_sec: int = 300) -> dict:
    start = time.time()

    result = {
        'scanned_root': str(scan_root),
        'repos': [],
        'stats': {
            'repos_scanned': 0,
            'files_parsed': 0,
            'agents_found': 0,
            'tools_resolved': 0,
            'errors': 0,
        },
    }

    for child in sorted(scan_root.iterdir()):
        if time.time() - start > overall_timeout_sec:
            break
        if not child.is_dir():
            continue
        repo_root = child

        agents, warnings, files_parsed = parse_repo(repo_root)

        tools_resolved = sum(
            sum(1 for t in a.get('tools_used', []) if t.get('defined_in') not in ('unknown', None))
            for a in agents
        )

        result['repos'].append({
            'repo_path': str(repo_root),
            'agents': agents,
            'warnings': [{'file': w.file, 'reason': w.reason} for w in warnings],
        })

        result['stats']['repos_scanned'] += 1
        result['stats']['files_parsed'] += files_parsed
        result['stats']['agents_found'] += len(agents)
        result['stats']['tools_resolved'] += tools_resolved
        result['stats']['errors'] += len([w for w in warnings if 'Error' in w.reason or 'SyntaxError' in w.reason])

    return result


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description='Parse CrewAI agents and tools from repos')
    parser.add_argument('--root', type=Path, default=DEFAULT_SCAN_ROOT, help='Root directory containing cloned repos (default: ./crewai-repos)')
    parser.add_argument('--timeout', type=int, default=300, help='Overall timeout seconds (default: 300)')
    args = parser.parse_args(argv)

    scan_root = args.root.resolve()
    if not scan_root.exists():
        print(json.dumps({
            'scanned_root': str(scan_root),
            'repos': [],
            'stats': {
                'repos_scanned': 0,
                'files_parsed': 0,
                'agents_found': 0,
                'tools_resolved': 0,
                'errors': 1,
            },
        }, ensure_ascii=False))
        return 1

    result = parse_all(scan_root, overall_timeout_sec=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))


