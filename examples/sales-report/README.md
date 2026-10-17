# Demo: sales report from workspace files

> **Synthetic data.** Everything in this folder is invented for demonstration: a fictional tea seller with made-up regions, sales and notes. It contains no real customer or business data.

This demo shows the normal Nature AI Worker flow with a real model: the agent reads files with its tools, analyzes the data, and writes `report.md` to the workspace.

| File | Contents |
|---|---|
| `sales.csv` | 36 rows: `month, region, product, units, revenue` for July–September 2026 |
| `notes.md` | Three short team notes |

## Run it

1. **Start Nature AI Worker** with this folder's files in a fresh workspace (from the repository root, after installing as in the main README):

   ```bash
   mkdir -p demo-workspace && cp examples/sales-report/sales.csv examples/sales-report/notes.md demo-workspace/
   NATURE_WORKER_WORKSPACE=demo-workspace NATURE_WORKER_DATA_DIR=demo-data nature-worker
   ```

   Open http://127.0.0.1:8080.

2. **Connect a model.** Choose a provider and a model that supports tool calling, click **Test connection**, then **Save**. Ollama with a local model needs no API key.

3. **Enter the task:**

   > Read sales.csv and notes.md. Calculate total revenue per region, per product and per month, and the overall total. Then write report.md with those totals as Markdown tables, the best and weakest region, and two sentences that relate the numbers to the team notes. Reply with a short summary.

4. **Watch the tool calls.** The agent typically uses `list_files`, `read_file`, `table_summary` and `calculate`. Click a tool card to see its arguments and result.

5. **Open the result.** The agent writes `report.md` with `write_file`. Open **Workspace files** to preview or download it.

The model decides how to word the report, so the text differs between models and runs. The numbers should not.

## Correct results

Use these to check the model's report:

| Region | Revenue |
|---|---|
| North | 10,680 |
| South | 8,450 |
| East | 12,914 |
| West | 10,130 |

| Product | Revenue |
|---|---|
| Green tea | 14,532 |
| Black tea | 10,660 |
| Herbal tea | 16,982 |

| Month | Revenue |
|---|---|
| 2026-07 | 15,954 |
| 2026-08 | 12,780 |
| 2026-09 | 13,440 |

Overall total: **42,174** revenue from **3,490** units. Best region: East. Weakest region: South.

If the report has different numbers, the model made a mistake. Smaller models are more likely to; use one with reliable tool calling.

## Recording a demo

Use your operating system's screen recorder or any recording tool you already have. Before you record:

- Enter the API key before you start recording, or set it as an environment variable. Do not show the Settings dialog with a key in it. With Ollama, no key is needed.
- Do not show private endpoint addresses or files other than this demo data.
- Record a real run. If the model makes a mistake, show it or try again; do not edit the result.
