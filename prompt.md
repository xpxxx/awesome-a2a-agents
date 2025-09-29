Give me a **repository parser** python script.

Scan **all Git repos under /crewai-repos** and extract **CrewAI agent definitions** and the **tools** they use.

## **What to find**

1. **Agents**

   - Defined via from crewai import Agent (or equivalent import forms).
   - Extract for each agent:
     - **name**: identifier of the agent instance (e.g., pricing_agent)
     - **role**: value of the role argument (string literal or raw snippet if dynamic)
     - **goal**: value of the goal argument (string literal or raw snippet if dynamic)
     - **llm**: language model configuration, annotated by which API it needs (e.g., "openai:gpt-4", "anthropic:claude-3", "ollama:llama2", etc.); if unresolved, set "unknown"
     - **tools_used**: array of tool objects following the **Tools Extraction Rules**

2. **Tools**

   - Tools may be:

     - **Class-based tools** (instantiated tool classes passed to agent configs)
     - **Function-based tools** defined in modules named like AgentTools, *_tools.py, or similar helper modules

     For each tool reference associated with an agent, capture the following attributes:

     - **name**: identifier used in code (e.g., ElasticTool, fetch_data)
     - **qualname**: fully qualified name (e.g., package.module.ClassOrFunc), if resolvable
     - **kind**: "class" | "function" | "unknown"
     - **defined_in**: absolute path if defined in the repo; "external" if from outside; "unknown" if unresolved
     - **docstring**: first line only, if available (else null)

### ***Two Sources of Tools***

1. **Developer-defined tools (inside repo)**

   - These are classes or functions implemented in the repository itself.

   - Attempt to resolve:

     - qualname (module + class/function name)
     - defined_in (absolute path to the source file)
     - docstring (first line if present)
     - kind (detect via AST if it’s a class or function)

     Example:

   ```json
   {
     "name": "fetch_market_signals",
     "qualname": "tools.agent_tools.fetch_market_signals",
     "kind": "function",
     "defined_in": "/crewai-repos/price-bot/tools/agent_tools.py",
     "docstring": "Fetches demand signals from the data lake."
   }
   ```

   2. **Imported from crewai_tools**

   - These are tool classes imported like from crewai_tools import WebSearchTool.

   - Since their source is external, **only name is reliably available**.

   - Record other fields with defaults:

     - qualname: null
     - kind: "class" (assume tool classes)
     - defined_in: "external"
     - docstring: null

     Example:

     ```json
     {
       "name": "WebSearchTool",
       "qualname": null,
       "kind": "class",
       "defined_in": "external",
       "docstring": null
     }
     ```

## **Detection rules & heuristics**

- **Agent detection**
  - Match Agent(...) calls (including keyword/positional args).
  - Handle imports like:
    - from crewai import Agent
    - import crewai (then crewai.Agent(...))
    - from crewai import Agent as Something
  - Accept both:
    - Direct assignment (my_agent = Agent(...))
    - Dict/list aggregations (agents = [Agent(...), ...])
    - Factories/wrappers returning Agent (record as class_or_factory="factory" if you can infer).
- **Tool detection**
  - In Agent(...), look for args commonly named tools, tool, toolkit, etc.
  - Resolve identifiers to:
    - **Class instantiations** (e.g., MySearchTool() → class)
    - **Bare function refs** (e.g., fetch_data → function)
  - If a module/class is named like AgentTools, Tools, or *_tools, treat its exported functions as potential tools when referenced.
  - When unresolved, still record the symbol with kind="unknown" and defined_in="unknown".
- **Parsing approach**
  - Prefer **AST parsing** (Python only). Do **not** execute repo code.
  - Be resilient to syntax errors (skip file with a warning).
  - Follow imports **within the same repo** to resolve qualname and defined_in.
  - Support multiple agents per file.
- **Scope**
  - Recurse under /crewai-repos/**.
  - Ignore: venv, .venv, node_modules, .git, __pycache__, build artifacts, large binary files.
  - Only parse .py files.
- **Performance & safety**
  - No network access, no package installs.
  - Use timeouts per file (e.g., 2s) and overall (e.g., 5m) with graceful degradation.
  - Memory-safe: stream results per repo and merge at the end.

## **Output format (JSON only)**

Return **one JSON object** with this exact shape:

```
{
  "scanned_root": "/crewai-repos",
  "repos": [
    {
      "repo_path": "/crewai-repos/<repo_name>",
      "agents": [
        {
          "name": "string",
          "role": "string|null",
          "goal": "string|null",
          "llm": "string|null",
          "tools_used": [
            {
              "name": "string",
              "qualname": "string|null",
              "kind": "class|function|unknown",
              "defined_in": "string|external|unknown",
              "docstring": "string|null"
            }
          ]
        }
      ],
      "warnings": [
        {
          "file": "string",
          "reason": "string"
        }
      ]
    }
  ],
  "stats": {
    "repos_scanned": 0,
    "files_parsed": 0,
    "agents_found": 0,
    "tools_resolved": 0,
    "errors": 0
  ]
}
```

