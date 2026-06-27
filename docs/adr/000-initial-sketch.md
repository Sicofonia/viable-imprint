Everything is now clear enough to proceed. Here's the architectural plan.

---

## Architecture Overview

### Philosophy
- **Dumb and sequential** — each command is independent; you run it, inspect output, then run the next
- **Filesystem as state** — the folder structure itself tracks progress; no database, no state files
- **Python orchestration** — thin CLI wrapper around modular scripts
- **Mistral API** for all LLM calls (your keys)
- **DeepL API** for translation (your key)
- **uno/LibreOffice** for .odt generation instead of OpenClaw (cleaner approach)

---

### Folder Structure

```
publishing-pipeline/
│
├── pipeline.py                  # CLI entry point
├── config.yaml                  # API keys, paths (gitignored)
├── config.example.yaml          # template for config
├── requirements.txt
│
├── agents/
│   ├── cleanup.py               # Agent: OCR cleanup + [] markup
│   ├── translate.py             # Agent: DeepL translation
│   ├── format_odt.py            # Agent: apply [] formatting to .odt
│   └── marketing.py             # Agent: AI sheet, press kit, web profile
│
├── lib/
│   ├── mistral_client.py        # Mistral API wrapper
│   ├── deepl_client.py          # DeepL API wrapper
│   └── odt_writer.py            # LibreOffice/uno formatting functions
│
├── prompts/
│   ├── cleanup_system.txt       # System prompt for cleanup agent
│   ├── marketing_system.txt     # System prompt for marketing agent
│   └── intelligence_system.txt  # System prompt for System 4 agent
│
└── books/                       # Your book projects live here
    └── life-as-explorer/
        ├── source/
        │   └── my-life-as-explorer.txt
        ├── cleaned/
        ├── translated/
        ├── formatted/
        └── marketing/
```

---

### CLI Commands

| Command | What it does |
|---------|--------------|
| `python pipeline.py cleanup "books/life-as-explorer/source/my-life-as-explorer.txt"` | Reads source file, sends to Mistral for OCR cleanup + `[]` markup, saves to `cleaned/` |
| `python pipeline.py translate "books/life-as-explorer/cleaned/my-life-as-explorer.txt"` | Reads cleaned file, calls DeepL, saves to `translated/` |
| `python pipeline.py format "books/life-as-explorer/cleaned/my-life-as-explorer.txt"` | Reads cleaned or translated file, generates .odt with formatting applied, saves to `formatted/` |
| `python pipeline.py marketing "books/life-as-explorer"` | Reads final manuscript, generates AI sheet + press kit + web profile in `marketing/` |
| `python pipeline.py intelligence` | System 4 agent: produces weekly 1-pager report, saves to a designated folder |
| `python pipeline.py policy "Does text X fall within our scope?"` | System 5 token agent: answers policy questions against the VSM doc |

---

### Agent Specifications

#### Cleanup Agent (System 1B)
- **Input:** Raw OCR text file
- **Mistral call:** System prompt defines editorial standards for historical texts (period spelling, common OCR artifacts like `rn`→`m`, `cl`→`d`, broken line breaks, page numbers, headers/footers). Outputs cleaned text with `[i]italic text[/i]` and `[sc]small caps text[/sc]` markup
- **Output:** Same filename in `cleaned/` folder
- **Spot-check:** You open the file, verify the first few pages, flag issues

#### Translation Agent (System 1B)
- **Input:** Cleaned text file
- **No LLM needed** — pure DeepL API call. Preserves `[i]` and `[sc]` markup by sending text in segments, respecting the tags
- **Output:** Same filename in `translated/` folder
- **Note:** This is essentially a Python script wrapping your DeepL key. If DeepL has trouble with markup preservation, we can add a lightweight Mistral call to realign tags post-translation

#### Formatting Agent (System 1B → 1C handoff)
- **Input:** Cleaned (or translated) text file with `[]` markup
- **No LLM needed** — Python script using `uno` to:
  1. Open a headless LibreOffice instance
  2. Create a new document
  3. Iterate through the text, applying italics where `[i]...[/i]` appears, small caps where `[sc]...[/sc]` appears
  4. Apply your predefined styles (body text, chapter headings, etc. — these can be defined in a template .ott file)
  5. Save as .odt in `formatted/`
- **Output:** `.odt` file ready for you to open, review, and eventually hand off to design

#### Marketing Agent (System 1D part)
- **Input:** Path to the book project folder (reads the final manuscript)
- **Mistral call:** System prompt instructs it to produce three markdown documents:
  - **Advanced Information Sheet:** title, author bio/historical context, synopsis, target audience, comparable titles, key selling points
  - **Press Kit:** boilerplate about the publisher, press release, author Q&A if applicable, high-res cover description
  - **Website Profile Page:** SEO-friendly description, pull quotes, "about the author," "why this book matters," reader testimonials placeholder
- **Output:** Three `.md` files in `marketing/`
- **Spot-check:** You review, tweak, then manually transfer to Canva templates

#### System 4 — Intelligence Agent
- **Trigger:** Manual CLI command
- **Mistral call:** The system prompt will ask Mistral to:
  - Scan its training knowledge for recent trends in historical travel literature publishing
  - Identify newly public-domain authors/works in the 1925–1930 range (works entering public domain)
  - Flag notable academic papers, exhibitions, or cultural events related to Asian exploration
  - Suggest 2–3 actionable ideas for the publisher
- **Important caveat:** Mistral models have knowledge cutoffs and cannot browse the live web. The report will be based on training data. If you need real-time scanning, we'd need a web search API integration (Brave, SerpAPI) — we can add that later
- **Output:** A single markdown file: `reports/intelligence-2026-05-07.md`

#### System 5 — Policy Agent
- **Trigger:** Manual CLI command
- **Mistral call:** The full VSM markdown you shared is its system prompt. You ask a question like "Does the travelogue of Alexandra David-Néel fall within our scope?" and it answers with reasoning citing your policy
- **Output:** Prints to terminal

---

### Technology Stack

| Component | Technology | Why |
|-----------|------------|-----|
| CLI framework | `click` or `argparse` | Lightweight, standard Python |
| LLM API | `mistralai` Python SDK or raw `requests` to Mistral | Your existing keys |
| Translation | `deepl` Python SDK | Your existing key |
| ODT generation | `uno` (LibreOffice Python bindings) | Already installed and working on your machine |
| Config | `yaml` (PyYAML) | Simple, readable, gitignorable |
| Prompts | Plain `.txt` files in `prompts/` | Easy to edit, version control, and iterate |

**No databases, no servers, no state management beyond the filesystem.**

---

### What Gets Me Under the Hood

For the cleanup and marketing agents to work well, I need to draft precise system prompts. A few inputs from you would help:

1. **Cleanup agent — examples:** Do you have a page or two of raw OCR output you could share? Not the whole book — just a representative sample showing typical artifacts (broken paragraphs, misrecognized characters, page headers, footnotes behaving badly). This lets me craft a prompt that actually works on your real material.

2. **Marketing agent — style guide:** For the AI sheet, press kit, and web profile — any examples of tone or format you like? Perhaps an existing AI sheet from another publisher you admire, or just a description of the voice (scholarly but accessible? adventure-narrative?).

3. **LibreOffice template:** Do you have a `.ott` template file you use for your books (margins, fonts, heading styles, body text style)? The formatting agent will need to know what styles to apply.

---

### What I'll Deliver

Once you confirm this architecture works for you (or flag what needs changing), I'll produce:

1. **`config.example.yaml`** — the structure for your API keys and paths
2. **`pipeline.py`** — the CLI with all commands
3. **`lib/mistral_client.py`** — thin wrapper for Mistral API calls
4. **`lib/deepl_client.py`** — thin wrapper for DeepL calls
5. **`lib/odt_writer.py`** — formatting script using uno
6. **`agents/cleanup.py`** — the cleanup agent
7. **`agents/translate.py`** — the translation agent
8. **`agents/format_odt.py`** — the formatting agent
9. **`agents/marketing.py`** — the marketing agent
10. **`prompts/cleanup_system.txt`** — the system prompt for cleanup
11. **`prompts/marketing_system.txt`** — the system prompt for marketing
12. **`prompts/intelligence_system.txt`** — the system prompt for intelligence reports
13. **`requirements.txt`** — all dependencies