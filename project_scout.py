#!/usr/bin/env python3
"""
project_scout.py — near-real-time GitHub project-idea alerts for students, sent to Telegram.

Three lanes per major (Quant, FinTech, SWE, Cyber, Data):
  🧪 build     repos CREATED in the last 14 days that already have stars = what people build now
  🤝 oss       active repos with open "good first issue" tickets = contribution opportunities
  🔬 research  fresh repos whose README cites arXiv = paper code / research to join or reproduce
Anything already sent is skipped (seen.json, 60-day TTL); sends nothing when nothing is new.
Runs every 2 hours in GitHub Actions.

  python project_scout.py            # send
  python project_scout.py --preview  # print instead of sending
  python project_scout.py --self-check

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, optional GITHUB_TOKEN (raises the
search rate limit from 10 to 30 req/min; Actions passes its built-in token).
stdlib only.
"""
import json
import shutil
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

SEEN_TTL_DAYS = 60
SEEN = "seen.json"

# major -> (label, OR-list search terms for the build lane, single anchor word for the other lanes, evergreen list)
MAJORS = {
    "quant": ("📈 Quant",
              '"quantitative finance" OR backtesting OR "algorithmic trading" OR "options pricing" OR "portfolio optimization"',
              "trading", "https://github.com/wilsonfreitas/awesome-quant"),
    "fintech": ("💳 Finance / FinTech",
                'fintech OR payments OR "personal finance" OR "open banking" OR "stock market" OR "financial data"',
                "finance", "https://github.com/topics/fintech"),
    "accounting": ("🧾 Accounting",
                   'accounting OR bookkeeping OR invoicing OR "double-entry" OR "financial statements" OR XBRL',  # 5 OR max: GitHub rejects a 6th operator
                   "accounting", "https://github.com/topics/accounting"),
    "swe": ("💻 Software Engineering",
            '"build your own" OR "from scratch" OR "project ideas" OR "portfolio project" OR "full stack"',
            "software", "https://github.com/codecrafters-io/build-your-own-x"),
    "cyber": ("🔐 Cybersecurity",
              'cybersecurity OR "penetration testing" OR "threat detection" OR "malware analysis" OR "security tool" OR CTF',
              "security", "https://github.com/sindresorhus/awesome#security"),
    "data": ("📊 Data Analytics",
             '"data analytics" OR "data analysis" OR "exploratory data analysis" OR "data pipeline" OR "data visualization"',
             "data", "https://github.com/academic/awesome-datascience"),
    "pm": ("📋 Project Management",
           '"project management" OR scrum OR kanban OR "task management" OR "agile" OR roadmap',
           '"project management"', "https://github.com/topics/project-management"),
    "marketing": ("📣 Digital Marketing",
                  '"digital marketing" OR SEO OR "marketing analytics" OR "social media" OR "email marketing" OR "growth hacking"',
                  "marketing", "https://github.com/topics/marketing"),
}


def ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# lane -> (label, picks per major, sort, query builder(terms, anchor))
# GitHub search has no parentheses, so only the build lane uses the OR list; others use the anchor word.
LANES = {
    "build": ("🧪 Build this", 2, "stars",
              lambda terms, anchor: f"{terms} created:>={ago(14)} stars:>=10 archived:false"),
    "oss": ("🤝 Contribute — open good-first-issues", 2, "updated",
            lambda terms, anchor: f"{anchor} good-first-issues:>0 stars:>=50 pushed:>={ago(30)} archived:false"),
    "research": ("🔬 Research — fresh paper code (cites arXiv)", 2, "stars",
                 lambda terms, anchor: f"{RESEARCH_ANCHOR[anchor]} {anchor} arxiv in:readme,description created:>={ago(30)} stars:>=20 archived:false"),
}
# Build-lane sub-queries for majors that need more than one search. (label, GitHub query text, picks per run)
# GitHub allows one `language:` per query, so languages are separate searches. SWE is weighted to Python + SQL.
# CIS majors (cyber, swe, data) are themed on PYTHON and SQL you can actually run: the searches are weighted that way
# and every slot keeps at least one SQL query, because SQL is the half students most often never practise. The
# non-technical majors (pm, marketing) and the finance majors are NOT themed this way -- they keep broad,
# quality-first sourcing, since a Python repo is rarely the best answer for them.
BUILD_QUERIES = {
    "swe": [("🐍 Python backend", '"backend" OR api OR "web app" OR cli OR automation language:Python', 2),
            ("🗄 SQL & databases", 'sql OR postgres OR sqlite OR "data model" OR analytics language:SQL', 1),
            ("🗄 SQL with Python", 'sql OR postgres OR sqlalchemy OR "data pipeline" language:Python', 1),
            ("🐍 Python practice", 'exercises OR katas OR "practice problems" OR "project ideas" language:Python', 1),
            ("🖥 Frontend", 'frontend OR react OR vue OR "web app" OR dashboard language:TypeScript', 1)],
    "data": [("📊 Analysis & pipelines", MAJORS["data"][1], 2),
             ("🐍 Pandas & notebooks", 'pandas OR notebook OR "exploratory data analysis" OR matplotlib language:Python', 1),
             ("🗄 SQL analytics", 'sql OR postgres OR duckdb OR dbt OR "window function" language:SQL', 1),
             ("📈 Power BI", '"power bi" OR powerbi OR DAX OR "power query"', 1),
             ("📉 Tableau", 'tableau OR "tableau public" OR tabpy OR hyper', 1)],
    "cyber": [("🐍 Python security scripting", 'detection OR "log analysis" OR forensics OR "incident response" language:Python', 2),
              ("🗄 SQL for security", 'osquery OR siem OR "threat hunting" OR "log queries" language:SQL', 1)],
}

# Cybersecurity by the 8 CISSP domains: live search terms + curated legit projects + a reference link each.
CYBER_DOMAINS = {
    "d1": ("D1 Security & Risk Management (GRC)", 'grc OR "risk management" OR compliance OR "security policy" OR "nist csf"',
           ["cisagov/cset", "usnistgov/OSCAL", "mitre/saf"], ("NIST Cybersecurity Framework", "https://www.nist.gov/cyberframework")),
    "d2": ("D2 Asset Security", '"data loss prevention" OR "secrets detection" OR "data classification" OR "asset inventory" OR "pii detection"',
           ["gitleaks/gitleaks", "trufflesecurity/trufflehog", "data-privacy-stack/presidio", "cisagov/ScubaGear", "hashicorp/vault"],
           ("CISA: Asset & data protection resources", "https://www.cisa.gov/resources-tools")),
    "d3": ("D3 Security Architecture & Engineering", '"threat modeling" OR cryptography OR "zero trust" OR "secure design" OR "secrets management"',
           ["OWASP/threat-dragon", "pyca/cryptography", "openbao/openbao", "sigstore/cosign"],
           ("OWASP Threat Modeling", "https://owasp.org/www-community/Threat_Modeling")),
    "d4": ("D4 Communication & Network Security", '"network security" OR firewall OR "intrusion detection" OR "packet capture" OR "network monitoring"',
           ["wireshark/wireshark", "zeek/zeek", "OISF/suricata", "nmap/nmap", "snort3/snort3"],
           ("Wireshark University / sample captures", "https://wiki.wireshark.org/SampleCaptures")),
    "d5": ("D5 Identity & Access Management", 'IAM OR authentication OR "single sign-on" OR OAuth OR passkeys OR "access control"',
           ["keycloak/keycloak", "goauthentik/authentik", "authelia/authelia", "ory/kratos", "oauth2-proxy/oauth2-proxy"],
           ("NIST SP 800-63 Digital Identity Guidelines", "https://pages.nist.gov/800-63-4/")),
    "d6": ("D6 Security Assessment & Testing", '"penetration testing" OR "vulnerability scanner" OR CTF OR fuzzing OR "security testing"',
           ["projectdiscovery/nuclei", "zaproxy/zaproxy", "OWASP/wstg", "juice-shop/juice-shop", "OWASP/Nettacker"],
           ("OWASP Web Security Testing Guide", "https://owasp.org/www-project-web-security-testing-guide/")),
    "d7": ("D7 Security Operations", 'SIEM OR "incident response" OR "threat hunting" OR "detection rules" OR "digital forensics" OR SOC',
           ["SigmaHQ/sigma", "elastic/detection-rules", "wazuh/wazuh", "Velocidex/velociraptor", "mitre-attack/attack-navigator"],
           ("MITRE ATT&CK", "https://attack.mitre.org/")),
    "d8": ("D8 Software Development Security", '"secure coding" OR SAST OR DevSecOps OR "dependency scanning" OR "supply chain security" OR sbom',
           ["OWASP/ASVS", "semgrep/semgrep", "aquasecurity/trivy", "OWASP/CheatSheetSeries", "dependency-check/DependencyCheck", "github/codeql"],
           ("OWASP Top 10 & ASVS", "https://owasp.org/www-project-application-security-verification-standard/")),
}


# Course-aligned searches. Quant = Baruch MFE curriculum (mfe.baruch.cuny.edu/curriculum, fetched 2026-09-15);
# Finance = Zicklin finance major core (FIN 3000/3610/3710) + the elective topics; PM = the tools TLDP asked for.
# One course per major per run rotates through the feed; `--courses` posts an evergreen "top repos per course" map.
COURSES = {
    "quant": [
        ("MTH 9814 Financial Markets & Securities", '"bond pricing" OR "yield curve" OR "financial instruments" OR "option payoff"'),
        ("MTH 9815 Software Engineering for Finance", '"trading system" OR "order book" OR "market data" language:C++'),
        ("MTH 9816 Fundamentals of Trading", '"algorithmic trading" OR "order execution" OR "order book" OR backtest'),
        ("MTH 9821 Numerical Methods for Finance", '"finite difference" OR "monte carlo" OR "binomial tree" OR "option pricing"'),
        ("MTH 9831 Probability & Stochastic Processes", '"stochastic calculus" OR "brownian motion" OR "stochastic process" OR "ito"'),
        ("MTH 9842 Optimization Techniques in Finance", '"portfolio optimization" OR "mean-variance" OR "quadratic programming" OR "efficient frontier"'),
        ("MTH 9845 Market & Credit Risk Management", '"value at risk" OR "expected shortfall" OR "credit risk" OR "risk model"'),
        ("MTH 9855 Asset Allocation & Portfolio Management", '"asset allocation" OR "black-litterman" OR "risk parity" OR "portfolio construction"'),
        ("MTH 9863 Volatility Filtering & Estimation", 'GARCH OR "realized volatility" OR "volatility estimation" OR "kalman filter"'),
        ("MTH 9866 FX Modeling & Market Making", '"foreign exchange" OR "market making" OR "fx options" OR "currency pairs"'),
        ("MTH 9873 / 9878 Interest Rate Models", '"interest rate model" OR "hull-white" OR "term structure" OR swaption'),
        ("MTH 9875 The Volatility Surface", '"volatility surface" OR "implied volatility" OR "local volatility" OR heston OR SABR'),
        ("MTH 9876 Credit Risk Models", '"credit default swap" OR "default probability" OR "merton model" OR "credit risk"'),
        ("MTH 9879 Market Microstructure Models", '"market microstructure" OR "limit order book" OR "order flow" OR "high frequency"'),
        ("MTH 9882 Fixed Income Risk Management", '"fixed income" OR duration OR convexity OR "bond portfolio"'),
        ("MTH 9887 Blockchain Technologies in Finance", 'solidity OR ethereum OR "smart contract" tutorial OR web3 tutorial'),
        ("MTH 9893 / 9867 Time Series & Algorithmic Trading", '"time series" OR ARIMA OR cointegration OR "pairs trading"'),
        ("MTH 9894 / 9897 Algorithmic & Systematic Trading", '"systematic trading" OR "trading strategy" OR backtesting OR "momentum strategy"'),
        ("MTH 9896 Behavioral Finance", '"behavioral finance" OR "investor sentiment" OR "sentiment analysis" stocks'),
        ("MTH 9898 / 9899 Data Science & Machine Learning in Finance", '"machine learning" finance OR "stock prediction" OR "factor model" OR "financial data"'),
    ],
    "fintech": [
        ("FIN 3000 Principles of Finance", '"time value of money" OR "capital budgeting" OR NPV OR IRR OR "financial calculator"'),
        ("FIN 3610 Corporate Finance", '"corporate finance" OR WACC OR "capital structure" OR "dividend policy" OR DCF'),
        ("FIN 3710 Investment Analysis", '"investment analysis" OR "portfolio theory" OR CAPM OR "security analysis" OR "efficient frontier"'),
        ("Financial modeling & valuation", '"financial modeling" OR "three statement" OR "DCF model" OR LBO OR "valuation model"'),
        ("Financial statement analysis", '"financial statements" OR "ratio analysis" OR "10-K" OR "SEC EDGAR" OR XBRL'),
        ("Derivatives & options", '"black-scholes" OR "option pricing" OR "options strategy" OR greeks'),
        ("Fixed income", '"fixed income" OR "bond valuation" OR "yield curve" OR duration'),
        ("International finance & FX", '"foreign exchange" OR "exchange rate" OR forex OR "currency hedging"'),
        ("Financial markets & trading", '"stock market" OR "market data" OR "trading platform" OR brokerage'),
        ("Personal finance & fintech apps", '"personal finance" OR budgeting OR "open banking" OR "robo-advisor" OR payments'),
        ("Risk management & credit", '"risk management" OR "credit scoring" OR "fraud detection" OR "credit risk"'),
        ("Real estate finance", '"real estate" OR mortgage OR amortization OR REIT'),
    ],
    "accounting": [
        ("Financial accounting & the ledger", '"double-entry" OR "general ledger" OR bookkeeping OR "chart of accounts"'),
        ("Financial statement analysis", '"financial statements" OR "income statement" OR "balance sheet" OR "ratio analysis"'),
        ("SEC filings & XBRL", 'XBRL OR "sec edgar" OR "10-K" OR "financial reporting"'),
        ("Managerial & cost accounting", '"cost accounting" OR "managerial accounting" OR budgeting OR "variance analysis"'),
        ("Auditing & internal controls", 'auditing OR "internal controls" OR "audit trail" OR "audit log" OR SOX'),
        ("Tax", '"tax calculator" OR "income tax" OR "sales tax" OR "tax filing" OR VAT'),
        ("Accounting information systems", '"accounting software" OR invoicing OR payroll OR "expense tracking" OR ERP'),
        ("Forensic accounting & fraud", '"fraud detection" OR benford OR "forensic accounting" OR "anomaly detection"'),
        ("Excel & spreadsheet automation", 'excel OR xlsx OR spreadsheet OR openpyxl OR "google sheets"'),
    ],
    "pm": [
        ("Scrum", 'scrum OR sprint OR "scrum master" OR "sprint planning" OR retrospective'),
        ("Jira", 'jira OR "jira api" OR "jira automation" OR "jira dashboard"'),
        ("Confluence", 'confluence OR "confluence api" OR atlassian OR "team wiki"'),
        ("Kanban", 'kanban OR "kanban board" OR "task board" OR "work in progress"'),
        ("Agile metrics & reporting", 'velocity OR burndown OR "agile metrics" OR "cycle time" OR "sprint report"'),
        ("Roadmaps & OKRs", 'roadmap OR OKR OR "product roadmap" OR "release planning"'),
        ("Requirements & user stories", '"user stories" OR "acceptance criteria" OR backlog OR "requirements management"'),
        ("Risk, stakeholders & schedules", '"risk register" OR stakeholder OR "project charter" OR gantt'),
    ],
}


def course_rotation(major: str) -> list[tuple[str, str, int]]:
    """One course per run per major; every course comes around every len(COURSES[major]) runs."""
    cs = COURSES[major]
    i = (datetime.now().timetuple().tm_yday * 4 + datetime.now().hour // 6) % len(cs)
    return [(f"🎓 {cs[i][0]}", cs[i][1], 1)]


# Case studies students can CONTRIBUTE to: community collections on GitHub (live open-issue counts) + legit
# non-GitHub challenges (links). Verified 2026-09-15; archived repos left out. "all" applies to every major.
CASES_REPO = "tldpprojectscout/tldp-case-studies"
CASES = {
    "all": [(CASES_REPO, "TLDP's own case library — every student contributes one case by spring (template inside)")],
    "data": [("rfordatascience/tidytuesday", "a new real dataset every Tuesday; submit your analysis by pull request"),
             ("https://www.opencasestudies.org/", "Johns Hopkins Open Case Studies — full data cases (repos at github.com/opencasestudies)"),
             ("https://www.makeovermonday.co.uk/", "Makeover Monday — weekly Tableau / Power BI challenge, publish yours"),
             ("https://www.workout-wednesday.com/", "Workout Wednesday — weekly Tableau and Power BI build challenges")],
    "cyber": [("redcanaryco/atomic-red-team", "one test per ATT&CK technique; add or fix an atomic"),
              ("SigmaHQ/sigma", "detection rules from real incidents; contribute a rule"),
              ("OWASP/CheatSheetSeries", "case-style secure-coding guidance; issues welcome newcomers"),
              ("center-for-threat-informed-defense/adversary_emulation_library", "full adversary emulation plans (case studies of real intrusions)"),
              ("MISP/misp-galaxy", "threat-actor and tool knowledge base; add or update entries"),
              ("https://thedfirreport.com/", "The DFIR Report — real incident write-ups to study (read-only)")],
    "swe": [("aosabook/aosabook", "The Architecture of Open Source Applications — chapter-length case studies of real systems"),
            ("donnemartin/system-design-primer", "system-design case studies; pull requests welcome"),
            ("https://hacktoberfest.com/", "Hacktoberfest (October) — four merged pull requests, counts as an event")],
    "pm": [("https://openpracticelibrary.com/", "Red Hat Open Practice Library — practices with worked examples; contribute via its GitHub"),
           ("https://guides.18f.gov/methods/", "18F Methods — US public-sector delivery methods (read-only)"),
           ("https://www.agilealliance.org/resources/experience-reports/", "Agile Alliance experience reports — real project retrospectives")],
    "marketing": [("PostHog/posthog.com", "product-analytics docs and tutorials; write a how-to"),
                  ("mautic/user-documentation", "marketing-automation docs; 150+ open issues"),
                  ("https://www.kaggle.com/datasets?search=marketing", "marketing datasets for your own case write-up")],
    "accounting": [("beancount/beancount", "double-entry in plain text; docs and bank-importer issues welcome newcomers"),
                   ("frappe/erpnext", "a full ERP with real accounting modules; open issues are well documented"),
                   ("https://www.sec.gov/edgar/search/", "SEC EDGAR full-text search — pick a filing and write your own case (read-only)")],
    "fintech": [("OpenBB-finance/OpenBB", "contribute an analysis or data integration to the open-source terminal"),
                ("https://www.kaggle.com/competitions?searchQuery=finance", "finance competitions with public write-ups")],
    "quant": [("QuantConnect/Lean", "the open backtesting engine; research and strategy contributions"),
              ("https://www.quantconnect.com/research", "community research posts — publish a strategy write-up"),
              ("https://www.kaggle.com/competitions?searchQuery=trading", "trading competitions with public notebooks")],
}


# ---- Learning paths: the year, in order, per major. Undergrad-friendly, verified 2026-09-15. --------------------
# Four stages that track the school year. Each item: (GitHub repo or URL, exactly what to do with it).
STAGES = [("1", "Sep–Oct", "Foundations — learn the tools by following lessons"),
          ("2", "Nov–Dec", "First projects — small, finishable, your own repo"),
          ("3", "Jan–Feb", "Contribute — your first merged pull request"),
          ("4", "Mar–May", "Capstone — one real project, written up as a case")]
PATHS = {
    "data": [
        [("microsoft/Data-Science-For-Beginners", "do the 10-week lessons (2 per week); each has a notebook and a quiz"),
         ("Asabeneh/30-Days-Of-Python", "if Python is new: one short day per day, do days 1–15")],
        [("rfordatascience/tidytuesday", "pick one week's dataset, make 3 charts, write 5 sentences of findings"),
         ("https://www.kaggle.com/competitions/titanic", "Kaggle Titanic: the classic first model, follow a public notebook then change one thing"),
         ("https://www.makeovermonday.co.uk/", "one Makeover Monday in Tableau or Power BI, publish it")],
        [("firstcontributions/first-contributions", "practice the fork → branch → pull request loop in 20 minutes, zero risk"),
         ("pandas-dev/pandas", "pick a 'good first issue' labeled docs; fix one docstring or example")],
        [("streamlit/streamlit", "build a dashboard on an NYC Open Data dataset; deploy it free on Streamlit Cloud"),
         ("tldpprojectscout/tldp-case-studies", "write it up as your case")],
    ],
    "swe": [
        [("microsoft/Web-Dev-For-Beginners", "24 lessons, build a terrarium, a typing game and a bank app along the way"),
         ("Asabeneh/30-Days-Of-Python", "Python side: days 1–20"),
         ("https://www.freecodecamp.org/learn/", "freeCodeCamp Responsive Web Design if you prefer guided exercises")],
        [("florinpop17/app-ideas", "build two Tier-1 apps (Bin2Dec, Border Radius Previewer…), then one Tier-2"),
         ("practical-tutorials/project-based-learning", "one guided project from the Python or JavaScript list, e.g. a to-do API")],
        [("firstcontributions/first-contributions", "your first pull request, 20 minutes"),
         ("TheAlgorithms/Python", "add tests or a docstring to one algorithm; maintainers are used to beginners"),
         ("public-apis/public-apis", "add or fix one API entry — a real merged PR")],
        [("florinpop17/app-ideas", "one Tier-3 app with a backend and a database (Python + SQL) — your capstone"),
         ("tldpprojectscout/tldp-case-studies", "write it up as your case")],
    ],
    "cyber": [
        [("microsoft/Security-101", "8 short lessons on the core concepts; do them before anything hands-on"),
         ("https://overthewire.org/wargames/bandit/", "OverTheWire Bandit levels 0–15: the Linux basics every cyber job assumes"),
         ("https://play.picoctf.org/", "picoCTF practice: 10 easy challenges, any category")],
        [("juice-shop/juice-shop", "run it locally (one Docker command), solve the 1-star and 2-star challenges"),
         ("https://tryhackme.com/", "TryHackMe 'Pre Security' then 'Intro to Cyber Security' paths (free rooms)")],
        [("firstcontributions/first-contributions", "your first pull request, 20 minutes"),
         ("OWASP/CheatSheetSeries", "fix or clarify one cheat sheet section; open a good-first-issue"),
         ("SigmaHQ/sigma", "write one detection rule for a technique you practiced")],
        [("redcanaryco/atomic-red-team", "build a small home lab, run 5 atomics, write the detections — your capstone"),
         ("tldpprojectscout/tldp-case-studies", "write it up as your case")],
    ],
    "quant": [
        [("Asabeneh/30-Days-Of-Python", "Python first: days 1–20"),
         ("microsoft/Data-Science-For-Beginners", "the pandas and visualization lessons (weeks 2–5)"),
         ("ranaroussi/yfinance", "pull 5 years of prices for 3 tickers, plot them, compute returns")],
        [("kernc/backtesting.py", "backtest a moving-average crossover in 30 lines; then break it on purpose and see why"),
         ("je-suis-tm/quant-trading", "read two simple strategies, reproduce one with your own tickers")],
        [("firstcontributions/first-contributions", "your first pull request, 20 minutes"),
         ("ranaroussi/yfinance", "fix a docs example or a small issue"),
         ("kernc/backtesting.py", "add an example notebook or test")],
        [("stefan-jansen/machine-learning-for-trading", "pick one chapter's notebook and extend it to new data — your capstone"),
         ("tldpprojectscout/tldp-case-studies", "write it up as your case")],
    ],
    "fintech": [
        [("Asabeneh/30-Days-Of-Python", "Python first: days 1–15"),
         ("microsoft/Data-Science-For-Beginners", "spreadsheets-to-pandas lessons (weeks 1–3)"),
         ("ranaroussi/yfinance", "pull prices and build a simple portfolio tracker in a notebook")],
        [("actualbudget/actual", "run it, import 3 months of your own (or sample) transactions, build 2 reports"),
         ("florinpop17/app-ideas", "build the Tier-1 'Calculator' then a loan-amortization calculator (TVM in code)")],
        [("firstcontributions/first-contributions", "your first pull request, 20 minutes"),
         ("firefly-iii/firefly-iii", "improve one documentation page or translate a string"),
         ("OpenBB-finance/OpenBB", "fix a docs example; browse 'good first issue'")],
        [("OpenBB-finance/OpenBB", "a DCF or comparables analysis notebook on 3 public companies using OpenBB data — your capstone"),
         ("tldpprojectscout/tldp-case-studies", "write it up as your case")],
    ],
    "accounting": [
        [("Asabeneh/30-Days-Of-Python", "Python first: days 1–15 — enough to read a CSV and loop over the rows"),
         ("jvns/pandas-cookbook", "the first three notebooks, redone on a spreadsheet you already have"),
         ("beancount/beancount", "record one month of transactions (yours or made up) as double-entry text; run the balance report")],
        [("frappe/books", "run it and book a small company's month: invoices, bills, payments — then export the trial balance"),
         ("dgunning/edgartools", "pull one company's 10-K, extract the income statement, compare three years in pandas")],
        [("firstcontributions/first-contributions", "your first pull request, 20 minutes"),
         ("beancount/beancount", "fix a docs example, or add an importer for a bank CSV format"),
         ("invoice-x/invoice2data", "add or fix a template so it parses an invoice it currently misses")],
        [("dgunning/edgartools", "ratio analysis of 5 companies in one industry straight from their XBRL filings, and what the numbers say — your capstone"),
         ("tldpprojectscout/tldp-case-studies", "write it up as your case")],
    ],
    "pm": [
        [("https://scrumguides.org/", "read the Scrum Guide (13 pages) and write a one-page summary in your own words"),
         ("https://openpracticelibrary.com/", "pick 5 practices; explain when you'd use each"),
         ("https://www.atlassian.com/agile", "Atlassian Agile Coach: Scrum, Kanban and Jira basics")],
        [("wekan/wekan", "run a Kanban board for a real 2-week task list (yours or a club's); track WIP"),
         ("makeplane/plane", "plan a 3-sprint project with issues, cycles and a roadmap; export the burndown")],
        [("firstcontributions/first-contributions", "your first pull request, 20 minutes"),
         ("makeplane/plane", "improve a docs page or reproduce and confirm a bug report"),
         ("https://www.atlassian.com/software/jira/free", "Jira free tier: run one of the team's projects with sprints and a Confluence page")],
        [("https://openpracticelibrary.com/", "manage a classmate's capstone in Jira: charter, backlog, 4 sprints, retros — your capstone"),
         ("tldpprojectscout/tldp-case-studies", "write it up as your case")],
    ],
    "marketing": [
        [("https://learndigital.withgoogle.com/digitalgarage", "Google Digital Garage fundamentals (free certificate)"),
         ("https://academy.hubspot.com/", "HubSpot Academy: Inbound Marketing and Email Marketing courses"),
         ("microsoft/Web-Dev-For-Beginners", "lessons 1–6 so you can build a landing page yourself")],
        [("umami-software/umami", "put analytics on a page you built; run a 2-week A/B headline test"),
         ("PostHog/posthog", "define 3 events and a funnel for a small site or app")],
        [("firstcontributions/first-contributions", "your first pull request, 20 minutes"),
         ("PostHog/posthog.com", "write one tutorial page for the docs"),
         ("mautic/user-documentation", "fix or add one documentation page")],
        [("PostHog/posthog", "a full campaign: landing page, analytics, email flow, results deck — your capstone"),
         ("tldpprojectscout/tldp-case-studies", "write it up as your case")],
    ],
}


# Further along? A challenge track per major — for students who arrive with experience or want to be stretched.
CHALLENGE = {
    "data": [("https://www.kaggle.com/competitions", "enter a live competition and beat the median score"),
             ("microsoft/ML-For-Beginners", "the full 12-week machine-learning course"),
             ("streamlit/streamlit", "ship a multi-page app with a database behind it")],
    "swe": [("codecrafters-io/build-your-own-x", "build your own interpreter, HTTP server or database from the list"),
            ("donnemartin/system-design-primer", "design one system end to end and write it up"),
            ("TheAlgorithms/Python", "implement an algorithm the repo doesn't have yet, with tests")],
    "cyber": [("https://ctftime.org/", "play a live CTF this month with a teammate"),
              ("redcanaryco/atomic-red-team", "run 10 atomics in a lab and write detections for each"),
              ("SigmaHQ/sigma", "get a detection rule merged")],
    "quant": [("QuantConnect/Lean", "write an algorithm on Lean, backtest it, paper-trade it"),
              ("stefan-jansen/machine-learning-for-trading", "reproduce a chapter on new data and report what changed")],
    "fintech": [("OpenBB-finance/OpenBB", "contribute a data integration or analysis command"),
                ("plaid/pattern", "extend the sample banking app with a new feature end to end")],
    "accounting": [("Arelle/Arelle", "validate a real XBRL filing and explain every error it reports"),
                   ("PSLmodels/Tax-Calculator", "model one tax-law change and report who it moves money to"),
                   ("erdogant/benfordslaw", "run Benford's law over a public transaction set and say what a fraud examiner would do next")],
    "pm": [("makeplane/plane", "run a real club or hackathon team through a full sprint cycle and publish the metrics"),
           ("https://www.pmi.org/certifications/certified-associate-capm", "start CAPM prep; use the capstone as your logged experience")],
    "marketing": [("PostHog/posthog", "instrument a real product and run a proper A/B experiment"),
                  ("mautic/mautic", "build a segmented automation flow and measure it")],
}


def render_path(key: str, repos: dict[str, dict | None]) -> str:
    cur = phase()[0]
    out = []

    def line(target, todo):
        r = repos.get(target)
        is_repo = "/" in target and not target.startswith("http")
        name = r["full_name"] if r else target.replace("https://", "").rstrip("/")
        url = r["html_url"] if r else (f"https://github.com/{target}" if is_repo else target)
        tag = f" ⭐{r['stargazers_count']}" if r else ""
        return f'  <a href="{url}">{esc(name)}</a>{tag}\n    {esc(todo)}'

    for i, (num, months, title) in enumerate(STAGES):
        marker = "▶" if i == min(cur, 3) or (cur == 0 and i == 0) else "•"
        out.append(f"\n<b>{marker} Stage {num} · {months} · {esc(title)}</b>")
        out += [line(t, todo) for t, todo in PATHS[key][i]]
    out.append("\n<b>🔥 Further along? Challenge track (any time)</b>\n<i>Already comfortable with a stage? Skip ahead, or take one of these on the side.</i>")
    out += [line(t, todo) for t, todo in CHALLENGE[key]]
    return "\n".join(out)


def path_repos(key: str) -> dict[str, dict | None]:
    targets = [t for stage in PATHS[key] for t, _ in stage] + [t for t, _ in CHALLENGE[key]]
    return {t: (gh_repo(t) if "/" in t and not t.startswith("http") else None) for t in targets}


def render_case(entry: tuple[str, str], repo: dict | None) -> str:
    target, blurb = entry
    if repo:
        return (f'• <a href="{repo["html_url"]}">{esc(repo["full_name"])}</a> ⭐{repo["stargazers_count"]} · {repo.get("open_issues_count", 0)} open issues · {difficulty(repo)}\n'
                f'  {esc(blurb)} · <a href="{repo["html_url"]}{GFI}">good first issues</a>')
    return f'• <a href="{target}">{esc(target.replace("https://", "").rstrip("/"))}</a>\n  {esc(blurb)}'


def cases_for(key: str) -> list[tuple[tuple[str, str], dict | None]]:
    out = []
    for entry in CASES.get(key, []) + CASES["all"]:
        out.append((entry, gh_repo(entry[0]) if "/" in entry[0] and not entry[0].startswith("http") else None))
    return out


def cyber_rotation() -> list[tuple[str, str, int]]:
    """Two CISSP domains per run (all eight covered every 4 runs) plus the general cyber search."""
    keys = list(CYBER_DOMAINS)
    i = (datetime.now().timetuple().tm_yday * 4 + datetime.now().hour // 6) * 2 % len(keys)
    picked = [keys[i], keys[(i + 1) % len(keys)]]
    return [("🔐 General", MAJORS["cyber"][1], 1)] + [(f"🔐 {CYBER_DOMAINS[k][0]}", CYBER_DOMAINS[k][1], 1) for k in picked]


# Fall/winter build lane: popular, still-maintained, beginner-oriented repos (not brand-new ones).
BEGINNER_Q = lambda terms, anchor: f"{terms} in:name,description,readme stars:>=200 pushed:>={ago(365)} archived:false"  # noqa: E731
# ---- Legitimacy filter (2026-09-16, after scam repos reached the feed) -------------------------------------------
# Live GitHub search returns star-farmed download traps ("Ghostfolio-…" clones under 30 sock-puppet owners, MT4/forex
# "strategies", wallet drainers, account harvesters). Every live result must pass ALL of these; curated tables are exempt.
import re as _re
SCAM_WORDS = _re.compile(
    r"\b(drainer|stealer|steal(s|ing)?|drain(s|ing)?|crack(s|ed|ing)?|keygen|nulled|cheat(s)?|aimbot|spoofer|harvest(er|ing)?|"
    r"account farm|free download|password|mediafire|mega\.nz|anonfiles|gofile|warez|torrent|captcha|turnstile|"
    r"non-repainting|mt4|mt5|metatrader|forex|profit sniper|signals? engine|viral|growth (framework|hack)|affiliate|backlink|funnel|"
    r"premium (free|unlocked)|unlocked version|activation|license key|casino|betting|adult|nsfw)\b", _re.I)
NON_EN = _re.compile(r"\b(für|und|der|die|das|mit|auf|nicht|eine?|para|con|una|los|las|des|les|une|avec|pour)\b", _re.I)
SOCK_OWNER = _re.compile(r"^[a-z]+\d{2,4}$", _re.I)  # lightningfast66, lisazhou886, elenahao66 …


def legit(it: dict, siblings: list[dict] | None = None) -> bool:
    """False for anything that looks like a download trap, star farm, scam, or non-code placeholder."""
    name, desc = it.get("full_name", ""), it.get("description") or ""
    owner, repo = (name.split("/") + [""])[:2]
    size, stars = it.get("size") or 0, it.get("stargazers_count") or 0
    if it.get("language") is None or size < 30:           # no code / a README-only shell
        return False
    if SCAM_WORDS.search(f"{name} {desc}") or NON_EN.search(desc):
        return False
    if stars > 100 and size < 60:                          # stars bought for a shell repo
        return False
    if SOCK_OWNER.match(owner) and stars < 200:            # throwaway "word+digits" accounts
        return False
    if siblings is not None and sum(1 for s in siblings if s.get("full_name", "").split("/")[-1].lower() == repo.lower()) >= 3:
        return False                                       # same repo name under many owners = clone farm
    return True


# Most new GitHub repos right now are LLM wrappers; keep them out of the non-software majors.
AI_SPAM = __import__("re").compile(r"\b(agents?|llms?|gpt|chatgpt|copilot|claude|openai|langchain|rag)\b", __import__("re").I)
AI_OK = {"swe", "data"}
# a bare word + arxiv returns generic AI repos; a field phrase keeps research on-major
RESEARCH_ANCHOR = {"trading": '"quantitative finance"', "finance": '"financial"', "software": '"software engineering"',
                   "security": "cybersecurity", "data": '"data analysis"', '"project management"': '"project management"',
                   "marketing": '"digital marketing"', "accounting": '"accounting"'}

# Mission-driven orgs whose GitHub repos welcome outside contributors (good-first-issues) — resume-ready experience.
# Each GitHub org -> display name. LinkedIn is login-walled, so rows link to a LinkedIn company search, not a scrape.
# Keep each sector <= 12 orgs: GitHub search queries max out at 256 chars.
ORGS = {
    "nonprofit": ("🌱 Non-profit", {"mozilla": "Mozilla", "OWASP": "OWASP", "wikimedia": "Wikimedia", "EFForg": "EFF",
                                    "datakind": "DataKind", "ushahidi": "Ushahidi", "hackforla": "Hack for LA",
                                    "codeforamerica": "Code for America",
                                    "torproject": "Tor Project",
                                    "freeCodeCamp": "freeCodeCamp", "BetaNYC": "BetaNYC (NYC civic tech)"}),
    "public": ("🏛 Public sector", {"cisagov": "CISA", "GSA": "US GSA", "18F": "18F", "usds": "US Digital Service",
                                   "nasa": "NASA", "usnistgov": "NIST", "CDCgov": "CDC",
                                   "CityOfNewYork": "City of New York", "NYCPlanning": "NYC Dept of City Planning",
                                   "alphagov": "UK GDS"}),
    "private": ("🏢 Private sector", {"microsoft": "Microsoft", "google": "Google", "aws": "AWS", "IBM": "IBM",
                                     "cloudflare": "Cloudflare", "elastic": "Elastic", "goldmansachs": "Goldman Sachs", "man-group": "Man Group",
                                     "jpmorganchase": "JPMorgan Chase", "bloomberg": "Bloomberg"}),
}
ORG_NAME = {o: n for _, orgs in ORGS.values() for o, n in orgs.items()}

# "Start here": curated, evergreen repos that give students project IDEAS and the basics to get going
# (idea lists, roadmaps, beginner courses, sample apps). Verified live 2026-09-15. Posted once per major
# with --starters; served on demand as /basics <major> (Telegram) and lane "Start here" (Discord).
STARTERS = {
    "quant": ["wilsonfreitas/awesome-quant", "stefan-jansen/machine-learning-for-trading", "je-suis-tm/quant-trading",
              "microsoft/qlib", "QuantConnect/Lean", "ranaroussi/yfinance"],
    "fintech": ["OpenBB-finance/OpenBB", "plaid/pattern", "stripe-samples/checkout-one-time-payments",
                "firefly-iii/firefly-iii", "actualbudget/actual", "ranaroussi/yfinance"],
    "swe": ["practical-tutorials/project-based-learning", "codecrafters-io/build-your-own-x", "florinpop17/app-ideas",
            "karan/Projects", "nilbuild/developer-roadmap", "ossu/computer-science"],
    "cyber": ["sbilly/awesome-security", "OWASP/CheatSheetSeries", "juice-shop/juice-shop", "OWASP/wstg",
              "swisskyrepo/PayloadsAllTheThings", "mitre-attack/attack-navigator"],
    "data": ["microsoft/Data-Science-For-Beginners", "jakevdp/PythonDataScienceHandbook", "Yorko/mlcourse.ai",
             "awesomedata/awesome-public-datasets", "streamlit/streamlit", "academic/awesome-datascience",
             "microsoft/PowerBI-Developer-Samples", "microsoft/powerbi-desktop-samples", "tableau/TabPy",
             "tableau/server-client-python", "tableau/hyper-api-samples"],
    "accounting": ["frappe/books", "beancount/beancount", "ledger/ledger", "akaunting/akaunting",
                   "invoiceninja/invoiceninja", "dgunning/edgartools", "Arelle/Arelle", "invoice-x/invoice2data"],
    "pm": ["dend/awesome-product-management", "opf/openproject", "makeplane/plane", "wekan/wekan",
           "mattermost-community/focalboard"],
    "marketing": ["PostHog/posthog", "umami-software/umami", "matomo-org/matomo", "mautic/mautic", "knadh/listmonk",
                  "n8n-io/n8n"],
}
# ---- the Python + SQL project ladder for the CIS majors -------------------------------------------------------------
# Hand-picked, verified repos a student can work through in order: 🟢 start here, 🟡 once you can code a bit, 🔴 a real
# piece of work. Each entry is (level, language, repo, what YOU build) — the repo is the material, the last field is the
# assignment, because a link with no task is not a project. Screened as curated evergreen rows like every other static
# set, so they are re-scanned monthly and dropped if one ever fails.
LEVELS3 = {"start": "🟢 Start here", "build": "🟡 Build on it", "deep": "🔴 Go deep"}
PROJECTS = {
    "cyber": [
        ("start", "Python", "OTRF/Security-Datasets",
         "Load one attack dataset into pandas and find the logon that does not belong. Write three sentences on how you spotted it."),
        ("start", "SQL", "osquery/osquery",
         "Install it, then query your OWN machine: running processes, listening ports, startup items. Save five queries you would run during an incident."),
        ("build", "Python", "SigmaHQ/sigma",
         "Write one Sigma rule for a technique you care about, then test it against the Security-Datasets logs above. Tune it until it stops firing on normal activity."),
        ("build", "Python", "thinkst/opencanary",
         "Run a honeypot on a spare port for a week. Chart what hit it, where from, and what it tried. That chart is your write-up."),
        ("deep", "Python", "volatilityfoundation/volatility3",
         "Take a public memory capture and list its processes, network connections and any injected code. Document what a defender would do next."),
        ("deep", "Python", "log2timeline/plaso",
         "Build a super-timeline from a public disk image and answer one question: what happened first?"),
    ],
    "swe": [
        ("start", "Python", "practical-tutorials/project-based-learning",
         "Pick ONE Python tutorial and finish it end to end — including the README and the tests the tutorial skips."),
        ("start", "Python", "Asabeneh/30-Days-Of-Python",
         "Work days 1–15, then build the day-30 project with your own data instead of the book's."),
        ("build", "Python", "TheAlgorithms/Python",
         "Pick three algorithms you cannot yet explain, reimplement them from the docstring alone, and write tests that prove yours matches."),
        ("build", "SQL", "lerocha/chinook-database",
         "Load Chinook and answer ten business questions with SQL. Then add an index and show the query plan before and after."),
        ("deep", "Python", "realworld-apps/realworld",
         "Build the RealWorld API backend in FastAPI or Django and pass the published test suite. This is a portfolio piece."),
        ("deep", "SQL", "sqlfluff/sqlfluff",
         "Lint a messy SQL folder, fix what it finds, then write one custom rule. A merged rule is a real open-source contribution."),
    ],
    "data": [
        ("start", "Python", "jvns/pandas-cookbook",
         "Work the notebooks, then redo chapter 3 on a CSV of your own. Post the before/after."),
        ("start", "SQL", "NUKnightLab/sql-mysteries",
         "Solve the SQL Murder Mystery, then write your own three-table mystery and make a classmate solve it."),
        ("build", "Python", "stefmolin/Hands-On-Data-Analysis-with-Pandas-2nd-edition",
         "Follow the cleaning and EDA chapters, then apply the same steps to a fivethirtyeight dataset and publish the notebook."),
        ("build", "SQL", "datacharmer/test_db",
         "Load the employees database and answer five HR questions (attrition, pay gaps, tenure) with windowed SQL."),
        ("deep", "Python", "ptyadana/SQL-Data-Analysis-and-Visualization-Projects",
         "Recreate one project end to end on a DIFFERENT dataset, and say where the original analysis would mislead someone."),
        ("deep", "SQL", "iweld/SQL_Coding_Challenge",
         "Complete the challenge, then rewrite your three slowest queries and show the timing difference."),
    ],
}


def gate_label() -> str:
    """The same "not a safety guarantee" line the gated feed uses (imported lazily: gate imports nothing from here)."""
    import gate
    return gate.LABEL


def projects_for(key: str) -> list[tuple[str, str, str, str]]:
    return PROJECTS.get(key, [])


def render_project(entry: tuple[str, str, str, str], repo: dict | None) -> str:
    level, lang, name, todo = entry
    head = f"{LEVELS3[level]} · {esc(lang)}"
    if repo:
        return (f'• {head} — <a href="{repo["html_url"]}">{esc(repo["full_name"])}</a> ⭐{repo["stargazers_count"]}\n'
                f"  🛠 {esc(todo)}")
    return f"• {head} — {esc(name)}\n  🛠 {esc(todo)}"


STARTER_WHY = {
    "quant": "idea list → book code with notebooks → example strategies → two real backtest engines → free market data",
    "fintech": "open-source Bloomberg-style terminal → bank-linking sample app → payments sample → two budgeting apps to study → market data",
    "swe": "project tutorials → build-your-own-X → app idea lists (easy/medium/hard) → roadmaps → a full CS curriculum",
    "cyber": "tools & resources list → OWASP cheat sheets → a deliberately vulnerable app to practice on → testing guide → payloads → ATT&CK map",
    "data": "beginner course → free textbook with notebooks → ML course → public datasets → dashboards in Python → resources list → Power BI samples (Microsoft) → Tableau TabPy, API client and Hyper samples",
    "accounting": "free accounting software you can actually run → plain-text double-entry (beancount, ledger) → invoicing to study → SEC filings and XBRL in Python → invoice data extraction",
    "pm": "PM resources list → three open-source PM tools to run, study or contribute to → kanban you can extend",
    "marketing": "product analytics → two web-analytics platforms → marketing automation → newsletters → workflow automation",
}


# ---- NYC in-person hackathons (spring requirement) --------------------------------------------------
# Sources with public data: Devpost's listing JSON (filtered to NYC-area in-person events) and Major League
# Hacking's season page (embeds event JSON). Everything else is login-walled, so it's linked, not scraped.
NYC_RE = __import__("re").compile(
    r"\b(new york, ?ny|new york, new york|nyc|brooklyn|manhattan|queens|bronx|staten island|flushing|jamaica, ny|jersey city|hoboken|newark, ?nj|"
    r"long island city|columbia university|nyu|cornell tech|cuny|baruch|fordham|pace university|stevens|stony brook|hofstra)\b", __import__("re").I)
# note: "<city>, New York" upstate (Ithaca, Troy, Rochester) is deliberately NOT matched — commutable NYC area only.
HACK_LINKS = [
    ("MLH season calendar (filter New York)", "https://mlh.io/seasons/2027/events"),
    ("Devpost — in-person hackathons", "https://devpost.com/hackathons?challenge_type[]=in-person&order_by=deadline&search=new+york"),
    ("Eventbrite NYC — hackathon", "https://www.eventbrite.com/d/ny--new-york/hackathon/"),
    ("Meetup NYC — hackathon", "https://www.meetup.com/find/?keywords=hackathon&location=us--ny--New%20York&source=EVENTS"),
    ("Luma NYC — hackathon", "https://lu.ma/nyc?q=hackathon"),
    ("NYC Civic Tech Hackathon (CUNY, annual)", "https://www.cuny.edu/civic-tech-hackathon/"),
    ("NASA Space Apps Challenge — NYC (October)", "https://www.spaceappschallenge.org/"),
    ("BetaNYC civic hack nights", "https://beta.nyc/events/"),
    ("NYC Open Data events", "https://opendata.cityofnewyork.us/events/"),
]


def _get(url: str, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 project-scout", "Accept": accept})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def hackathons_nyc() -> list[dict]:
    """Upcoming/open in-person hackathons in the NYC area, newest deadline first. Fields: title,url,when,where,org,src."""
    out, seen_urls = [], set()
    try:  # Devpost: page through in-person listings, keep NYC-area locations
        for page in range(1, 6):
            data = json.loads(_get("https://devpost.com/api/hackathons?status[]=open&status[]=upcoming&challenge_type[]=in-person"
                                   f"&order_by=deadline&per_page=50&page={page}"))
            hs = data.get("hackathons", [])
            if not hs:
                break
            for h in hs:
                loc = (h.get("displayed_location") or {}).get("location", "")
                if NYC_RE.search(loc) or NYC_RE.search(h["title"]):
                    out.append({"title": h["title"], "url": h["url"], "when": h.get("submission_period_dates", ""),
                                "where": loc, "org": h.get("organization_name") or "", "src": "Devpost",
                                "prize": h.get("prize_amount") or ""})
    except Exception as e:
        print(f"devpost: {e}", file=sys.stderr)
    try:  # MLH: the season page embeds one JSON object per event
        import re
        html = _get("https://mlh.io/seasons/2027/events", "text/html").decode("utf-8", "replace")
        for m in re.finditer(r'\{"id":"[0-9a-f-]{36}","slug":.*?"venueAddress":\{[^}]*\}\}', html):
            try:
                ev = json.loads(m.group(0))
            except ValueError:
                continue
            va = ev.get("venueAddress") or {}
            where = ev.get("location") or f"{va.get('city', '')}, {va.get('state', '')}"
            if ev.get("formatType") in ("physical", "hybrid") and NYC_RE.search(where):
                out.append({"title": ev["name"], "url": "https://mlh.io" + (ev.get("url") or "/seasons/2027/events"),
                            "when": ev.get("dateRange", ""), "where": where, "org": "MLH", "src": "MLH", "prize": ""})
    except Exception as e:
        print(f"mlh: {e}", file=sys.stderr)
    uniq = []
    for h in out:
        if h["url"] not in seen_urls:
            seen_urls.add(h["url"])
            uniq.append(h)
    return uniq


def render_hack(h: dict) -> str:
    prize = f" · 🏆 {esc(h['prize'])}" if h.get("prize") else ""
    org = f" · {esc(h['org'])}" if h.get("org") and h["org"] != h["src"] else ""
    return (f'• <a href="{h["url"]}">{esc(h["title"])}</a>{prize}\n'
            f"  📅 {esc(h['when'])} · 📍 {esc(h['where'])}{org} · via {h['src']}")


def hack_footer() -> str:
    links = " · ".join(f'<a href="{u}">{esc(n)}</a>' for n, u in HACK_LINKS[:5])
    return f"\n  🔎 More: {links}"


def gh_repo(full_name: str) -> dict | None:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "project-scout"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{full_name}", headers=headers), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"{full_name}: {e.code}", file=sys.stderr)
        return None


def starters() -> list[tuple[str, str]]:
    """One evergreen 'Start here' chunk per major: ideas + basics to get going."""
    chunks = []
    for key, (label, *_rest) in MAJORS.items():
        repos = ordered([r for r in (gh_repo(f) for f in STARTERS[key]) if r])
        rows = "\n".join(render_repo(r, "build") for r in repos)
        chunks.append((key, f"\n<b>📚 Start here — {label}: ideas &amp; basics</b>\n<i>{esc(STARTER_WHY[key])}</i>\n{rows}\n"
                            "  💡 Pick one, read its README, then run /scout for something fresh to build on top of it."))
    return chunks


def org_query(orgs: dict, anchor: str = "") -> str:
    return f"{anchor} {' '.join('org:' + o for o in orgs)} good-first-issues:>0 archived:false pushed:>={ago(90)}".strip()


def linkedin(name: str) -> str:
    return "https://www.linkedin.com/search/results/companies/?keywords=" + urllib.parse.quote(name)


# ---- GitHub budget: stay well inside the limits (30 search req/min with a token, 10 without) ----------------
SEARCH_BUDGET = int(os.environ.get("SEARCH_BUDGET", 26))  # hard cap per run; the digest stops when it's spent (majors rotate, so all get covered)
SEARCH_SPACING = 2.5        # seconds between searches → max 24/min even with no other pacing
_calls = {"n": 0, "limited": 0}


class BudgetExceeded(Exception):
    pass


def gh_search(q: str, sort: str = "stars", n: int = 15) -> list[dict]:
    import time
    if _calls["n"] >= SEARCH_BUDGET:
        raise BudgetExceeded(f"search budget {SEARCH_BUDGET} spent")
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": q, "sort": sort, "order": "desc", "per_page": n})
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "project-scout (github.com/tldpprojectscout/project-scout)"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    time.sleep(SEARCH_SPACING)
    _calls["n"] += 1
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return json.load(r).get("items", [])
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):  # rate-limited: back off exactly as GitHub asks, once; never hammer
            _calls["limited"] += 1
            wait = int(e.headers.get("Retry-After") or 0) or max(0, int(e.headers.get("X-RateLimit-Reset") or 0) - int(time.time()))
            if 0 < wait <= 90 and _calls["limited"] == 1:
                print(f"github rate limit: waiting {wait}s once", file=sys.stderr)
                time.sleep(wait + 1)
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    return json.load(r).get("items", [])
            raise BudgetExceeded(f"GitHub rate-limited us ({e.code}); stopping this run early")
        raise


def rate_limit_status() -> str:
    """Remaining search quota (free call, not counted). Used for the end-of-run report."""
    try:
        headers = {"User-Agent": "project-scout"}
        if os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
        with urllib.request.urlopen(urllib.request.Request("https://api.github.com/rate_limit", headers=headers), timeout=20) as r:
            s = json.load(r)["resources"]["search"]
            return f"search quota {s['remaining']}/{s['limit']} left"
    except Exception as e:
        return f"rate_limit check failed: {e}"


# ---- index.json: everything the feed found, served to the Workers from GitHub's CDN (raw.githubusercontent) -----
# Students' searches read this snapshot instead of the API, so their traffic never counts against GitHub limits.
INDEX = "index.json"
INDEX_TTL_DAYS, INDEX_ROWS_PER_KEY, STATIC_REFRESH_DAYS = 45, 30, 7
_index: dict = {}


def row(it: dict) -> dict:
    return {"full_name": it["full_name"], "html_url": it["html_url"], "stargazers_count": it.get("stargazers_count") or 0,
            "language": it.get("language"), "description": (it.get("description") or "")[:200], "size": it.get("size") or 0,
            "pushed_at": (it.get("pushed_at") or "")[:10], "created_at": (it.get("created_at") or "")[:10],
            "open_issues_count": it.get("open_issues_count", 0), "seen_at": date.today().isoformat()}


def load_index() -> dict:
    global _index
    try:
        _index = json.load(open(INDEX, encoding="utf-8"))
    except (OSError, ValueError):
        _index = {}
    _index.setdefault("keys", {})
    _index.setdefault("static", {})
    return _index


def index_add(key: str, items: list[dict]) -> None:
    """Merge search results under key "major|lane|sublabel"; newest first, de-duped, capped, 45-day TTL."""
    rows = _index["keys"].setdefault(key, [])
    cutoff = ago(INDEX_TTL_DAYS)
    fresh = [row(it) for it in items if english(f'{it["full_name"]} {it.get("description") or ""}') and (it.get("description") or "").strip()
             and legit(it, items)]
    names = {r["full_name"] for r in fresh}
    kept = [r for r in rows if r["full_name"] not in names and r.get("seen_at", "") >= cutoff and legit(r, rows)]  # re-vet carry-overs too
    _index["keys"][key] = (fresh + kept)[:INDEX_ROWS_PER_KEY]


def refresh_static() -> None:
    """Weekly: curated sets (Start here, CISSP domains) via the core API (5000/h, not the search limit)."""
    import time
    st = _index["static"]
    if st.get("refreshed_at", "") >= ago(STATIC_REFRESH_DAYS):
        return
    starters, domains = {}, {}
    for key, names in STARTERS.items():
        starters[key] = [row(r) for r in (gh_repo(n) for n in names) if r]
        time.sleep(0.3)
    for k, (label, _t, names, ref) in CYBER_DOMAINS.items():
        domains[k] = {"label": label, "ref": list(ref), "repos": [row(r) for r in (gh_repo(n) for n in names) if r]}
        time.sleep(0.3)
    cases = {}
    for key in list(MAJORS) + ["all"]:
        cases[key] = [{"target": e[0], "blurb": e[1], "repo": row(r) | {"open_issues_count": r.get("open_issues_count", 0)} if r else None}
                      for e, r in (((e, gh_repo(e[0]) if "/" in e[0] and not e[0].startswith("http") else None) for e in CASES.get(key, [])))]
        time.sleep(0.3)
    paths = {}
    for key in PATHS:
        repos = path_repos(key)
        paths[key] = [[{"target": t, "todo": todo, "repo": row(repos[t]) if repos.get(t) else None} for t, todo in stage] for stage in PATHS[key]]
        paths[key].append([{"target": t, "todo": todo, "repo": row(repos[t]) if repos.get(t) else None} for t, todo in CHALLENGE[key]])  # 5th = challenge track
        time.sleep(0.3)
    projects = {}
    for key, entries in PROJECTS.items():
        projects[key] = [{"level": lv, "lang": lang, "name": name, "todo": todo, "repo": row(r) if r else None}
                         for lv, lang, name, todo in entries for r in [gh_repo(name)]]
        time.sleep(0.3)
    st.update({"refreshed_at": date.today().isoformat(), "starters": starters, "cyber_domains": domains, "cases": cases,
               "paths": paths, "stages": STAGES, "projects": projects})


def save_index() -> None:
    _index["generated"] = datetime.now().isoformat(timespec="minutes")
    _index["phase"] = list(phase())
    json.dump(_index, open(INDEX, "w", encoding="utf-8"), separators=(",", ":"), ensure_ascii=False)


def load_seen() -> dict:
    try:
        seen = json.load(open(SEEN))
    except (OSError, ValueError):
        return {}
    cutoff = ago(SEEN_TTL_DAYS)
    return {k: v for k, v in seen.items() if k == "_meta" or v >= cutoff}


def english(desc: str) -> bool:
    """English-only: require a description and >= 90% ASCII (drops CJK, Cyrillic, mixed-language repos)."""
    return bool(desc.strip()) and sum(ord(c) < 128 for c in desc) / len(desc) >= 0.9


def difficulty(it: dict) -> str:
    """Rough on-ramp hint from repo size (KB) and stars — so freshmen don't pick a 20k-star monorepo."""
    size, stars = it.get("size") or 0, it.get("stargazers_count") or 0
    if size < 5000 and stars < 500:
        return "🟢 starter"
    if size < 50000 and stars < 5000:
        return "🟡 intermediate"
    return "🔴 advanced"


RANK = {"🟢 starter": 0, "🟡 intermediate": 1, "🔴 advanced": 2}
PHASES = [(0, "Phase 1 · Sep–Oct · starter projects"), (1, "Phase 2 · Nov–Jan · starter + intermediate"),
          (2, "Phase 3 · Feb–May · everything, including advanced")]


def phase(today: date | None = None) -> tuple[int, str]:
    """Difficulty ceiling rises through the school year so students ease in: 0 starter, 1 intermediate, 2 advanced."""
    m = (today or date.today()).month
    return PHASES[0] if m in (9, 10) else PHASES[1] if m in (11, 12, 1) else PHASES[2]


def ordered(items: list[dict], ceiling: int | None = None) -> list[dict]:
    """Easiest first (starter → intermediate → advanced), then most stars. With a ceiling, harder repos come last
    and are used only if nothing easier exists."""
    ranked = sorted(items, key=lambda it: (RANK[difficulty(it)], -(it.get("stargazers_count") or 0)))
    if ceiling is None:
        return ranked
    easy = [it for it in ranked if RANK[difficulty(it)] <= ceiling]
    return easy + [it for it in ranked if RANK[difficulty(it)] > ceiling]


def deep_ok(it: dict) -> bool | None:
    """Gate at post time: the deep vetter (contents, README links, history, owner) must pass before anything is
    shown to students. Verdicts are cached in vet_cache.json (committed by the workflow), so each repo costs ~4 core
    API calls once a month. Curated tables are exempt; this only runs on live search results."""
    global _vet_cache
    if "--self-check" in sys.argv or os.environ.get("NO_DEEP_VET"):
        return True
    try:
        import vet
    except ImportError:
        return True
    if _vet_cache is None:
        _vet_cache = vet.load_cache()
    v = _vet_cache.get(it["full_name"])
    if v is None or v.get("unknown"):
        v = vet.vet(it["full_name"])
        if v.get("unknown"):
            return None  # unreachable right now: skip this run, try again next time (not cached, not marked seen)
        _vet_cache[it["full_name"]] = v
        json.dump(_vet_cache, open(vet.CACHE, "w", encoding="utf-8"), indent=0)
    if not v["ok"]:
        print(f"vet: dropped {it['full_name']}: {'; '.join(v['hard'] or v['soft'])}", file=sys.stderr)
        return False
    return semgrep_ok(it)


_scan_cache = None


def semgrep_ok(it: dict) -> bool | None:
    """Layer 2 of the gate: shallow-clone and run the malware-behavior Semgrep ruleset (scan.py). Only runs where
    semgrep is installed (the Actions runners); a laptop preview without it just skips this layer. Verdicts are cached
    in scan_cache.json. Returns None when the clone/scan could not complete (try again next run, not marked seen)."""
    global _scan_cache
    if not shutil.which("semgrep") or not shutil.which("git"):
        return True
    import scan
    if _scan_cache is None:
        _scan_cache = {k: v for k, v in scan.load(scan.CACHE).items()
                       if v.get("checked", "") >= (date.today() - timedelta(days=30)).isoformat()}
    v = _scan_cache.get(it["full_name"])
    if v is None:
        v = scan.scan_repo(it["full_name"], it.get("size"))
        if v.get("ok") is None and not v.get("checked"):
            print(f"semgrep: {it['full_name']} {v.get('note')} (retry next run)", file=sys.stderr)
            return None
        _scan_cache[it["full_name"]] = v
        json.dump(_scan_cache, open(scan.CACHE, "w", encoding="utf-8"), indent=0)
        if v["ok"] is False:  # record in the vet cache too, so the weekly prune and the report see it
            _vet_cache[it["full_name"]] = {"ok": False, "hard": ["semgrep: " + ", ".join(v["hits"])], "soft": [],
                                           "stars": it.get("stargazers_count", 0), "checked": date.today().isoformat()}
            json.dump(_vet_cache, open("vet_cache.json", "w", encoding="utf-8"), indent=0)
    if v["ok"] is False:
        print(f"semgrep: dropped {it['full_name']}: {', '.join(v['hits'])}", file=sys.stderr)
        return False
    return True


_vet_cache = None


def pick(items: list[dict], seen: dict, n: int, major: str = "", hard: bool = False) -> list[dict]:
    """hard=True: the challenge pick — intermediate/advanced only, hardest first, regardless of phase."""
    out = []
    pool = ([it for it in ordered(items) if RANK[difficulty(it)] >= 1][::-1] if hard else ordered(items, phase()[0]))
    for it in pool:
        if it["full_name"] in seen or not english(f'{it["full_name"]} {it.get("description") or ""}') or not (it.get("description") or "").strip():
            continue
        if not legit(it, items):
            continue
        if major not in AI_OK and AI_SPAM.search(f"{it['full_name']} {it.get('description') or ''}"):
            continue
        ok = deep_ok(it)
        if ok is None:
            continue  # scanner could not reach it this run: leave it unseen, try again next time
        if not ok:
            seen[it["full_name"]] = date.today().isoformat()  # never look at it again
            continue
        seen[it["full_name"]] = date.today().isoformat()
        out.append(it)
        if len(out) == n:
            break
    return out


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


GFI = '/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22'


def render_repo(it: dict, lane: str) -> str:
    desc = (it.get("description") or "").strip()
    desc = desc[:140] + "…" if len(desc) > 140 else desc
    lang = f" · {it['language']}" if it.get("language") else ""
    tail = f'\n  👉 <a href="{it["html_url"]}{GFI}">open good-first-issues</a>' if lane == "oss" else ""
    link = it.get("commit_url") or it["html_url"]  # reviewed commit when screened, else the repo
    LVL = {"beginner": "🟢 Beginner", "intermediate": "🟡 Intermediate", "challenge": "🟠 Undergraduate Challenge"}
    lvl_label = LVL.get(it.get("level"))
    lm = it.get("level_meta") or {}
    level_line = f'\n  🎓 {lvl_label} · {esc(lm.get("effort", ""))} · task: {esc(lm.get("task", ""))}' if lvl_label else ""
    return (f'• <a href="{link}">{esc(it["full_name"])}</a> ⭐{it["stargazers_count"]}{lang} · {lvl_label or difficulty(it)}\n'
            f"  {esc(desc) or '(no description)'}{level_line}{tail}")


def render_org(it: dict, sector_label: str) -> str:
    org = it["full_name"].split("/")[0]
    name = ORG_NAME.get(org, org)
    return (render_repo(it, "oss") +
            f'\n  🏷 {sector_label} · {esc(name)} · <a href="{linkedin(name)}">LinkedIn</a>'
            f'\n  📝 Resume: Open-Source Contributor, {esc(name)} ({esc(it["full_name"].split("/")[1])})')


def build_digest(seen: dict) -> list[tuple[str, str]]:
    """(key, chunk) per major/orgs section that has unseen repos; empty list = nothing new, send nothing."""
    chunks = []
    run_no = datetime.now().timetuple().tm_yday * 4 + datetime.now().hour // 6   # 4 runs a day
    order = list(MAJORS)
    order = order[run_no % len(order):] + order[:run_no % len(order)]              # rotate who goes first, so the budget cap is fair
    for key in order:
        label, terms, anchor, evergreen = MAJORS[key]
        parts = []
        for lane, (lane_label, n, sort, q) in LANES.items():
            if lane in ("oss", "research") and (lane == "oss") != (run_no % 2 == 0):
                continue  # oss and research alternate runs → half the calls, still every 12 h each
            if lane == "research" and phase()[0] < 2:
                n, lane_label = 1, lane_label + " — for the further along"  # kept, but one pick and clearly labelled
            subs = ([(None, terms, n)] if lane != "build" else
                    cyber_rotation() if key == "cyber" else BUILD_QUERIES.get(key, [(None, terms, n)]))
            hard_sub = None
            if lane == "build" and phase()[0] < 2:
                # Fall/winter: established, documented, beginner-oriented repos first — plus one challenge pick.
                q, sort, lane_label = BEGINNER_Q, "stars", "🌱 Beginner-friendly & well documented"
                subs = [("learn", f"{anchor} beginner", 1), ("tutorial", f"{anchor} tutorial", 1)]
                hard_sub = ("🔥 Challenge — further along?", terms, 1)
            if lane == "build" and len(subs) > 3:                                   # e.g. SWE's 5 languages: 3 per run, rotating
                subs = [subs[(run_no + i) % len(subs)] for i in range(3)]
            if lane == "build" and key in COURSES:  # one course-aligned search per run
                subs = [(s[0], s[1], 1) for s in subs] + course_rotation(key)
            for sub_label, sub_terms, sub_n in subs:
                try:
                    found = gh_search(q(sub_terms, anchor), sort)
                    index_add(f"{key}|{lane}|{sub_label or ''}", found)
                    picks = pick(found, seen, sub_n, key)
                except BudgetExceeded as e:
                    print(f"stopping early: {e}", file=sys.stderr)
                    return finish(chunks, seen)
                except Exception as e:  # one bad query must not kill the run
                    print(f"{key}/{lane}/{sub_label}: {e}", file=sys.stderr)
                    continue
                if picks:
                    head = f"<i>{lane_label}{' · ' + sub_label if sub_label else ''}</i>"
                    parts.append(head + "\n" + "\n".join(render_repo(p, lane) for p in picks))
            if hard_sub:  # one intermediate/advanced repo per drop so nobody is bored
                try:
                    found = gh_search(BEGINNER_Q(hard_sub[1], anchor), "stars")
                    index_add(f"{key}|build|challenge", found)
                    picks = pick(found, seen, hard_sub[2], key, hard=True)
                    if picks:
                        parts.append(f"<i>{hard_sub[0]}</i>\n" + "\n".join(render_repo(p, "build") for p in picks))
                except BudgetExceeded as e:
                    print(f"stopping early: {e}", file=sys.stderr)
                    return finish(chunks, seen)
                except Exception as e:
                    print(f"{key}/challenge: {e}", file=sys.stderr)
        if parts:
            chunks.append((key, f"\n<b>{label}</b>\n" + "\n".join(parts) + f'\n  📚 <a href="{evergreen}">evergreen idea list</a>'))
    return finish(chunks, seen)


def full_index() -> None:
    """Every search the feed can make, once, into index.json (~85 searches ≈ 4 min at SEARCH_SPACING). Weekly job."""
    for key, (label, terms, anchor, evergreen) in MAJORS.items():
        subs = [(None, terms, 0)] if key not in BUILD_QUERIES and key != "cyber" else []
        subs += BUILD_QUERIES.get(key, [])
        if key == "cyber":
            subs += [("🔐 General", terms, 0)] + [(f"🔐 {d[0]}", d[1], 0) for d in CYBER_DOMAINS.values()]
        subs += [(f"🎓 {c[0]}", c[1], 0) for c in COURSES.get(key, [])]
        for sub_label, sub_terms in (("learn", f"{anchor} beginner"), ("tutorial", f"{anchor} tutorial"), ("starter", f"{anchor} starter project"), ("challenge", terms)):
            try:  # beginner-oriented keys the Worker prefers in fall/winter
                index_add(f"{key}|build|{sub_label}", gh_search(BEGINNER_Q(sub_terms, anchor), "stars"))
            except BudgetExceeded as e:
                print(f"full_index stopped: {e}", file=sys.stderr)
                return
            except Exception as e:
                print(f"{key}/build/{sub_label}: {e}", file=sys.stderr)
        for lane, (lane_label, n, sort, q) in LANES.items():
            for sub_label, sub_terms, _n in (subs if lane == "build" else [(None, terms, 0)]):
                try:
                    index_add(f"{key}|{lane}|{sub_label or ''}", gh_search(q(sub_terms, anchor), sort))
                except BudgetExceeded as e:
                    print(f"full_index stopped: {e}", file=sys.stderr)
                    return
                except Exception as e:
                    print(f"{key}/{lane}/{sub_label}: {e}", file=sys.stderr)
    for sector, (sector_label, orgs) in ORGS.items():
        try:
            index_add(f"orgs|{sector}|{sector_label}", gh_search(org_query(orgs), "updated"))
        except Exception as e:
            print(f"orgs/{sector}: {e}", file=sys.stderr)
    _index["hackathons"] = {"at": datetime.now().isoformat(timespec="minutes"), "items": hackathons_nyc()}


def merged_cases(seen: dict) -> list[dict]:
    """Newly merged pull requests in the TLDP case library (1 core-API call; read-only public data)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"https://api.github.com/repos/{CASES_REPO}/pulls?state=closed&sort=updated&direction=desc&per_page=20",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "project-scout",
                         **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {})}), timeout=30) as r:
            prs = json.load(r)
    except Exception as e:
        print(f"cases repo: {e}", file=sys.stderr)
        return []
    out = []
    for pr in prs:
        if pr.get("merged_at") and f"case:{pr['number']}" not in seen:
            seen[f"case:{pr['number']}"] = date.today().isoformat()
            out.append(pr)
    return out


def finish(chunks: list, seen: dict) -> list[tuple[str, str]]:
    """Sections that don't depend on the per-major loop: merged cases, hackathons (no GitHub) and mission-driven orgs."""
    for pr in merged_cases(seen):
        chunks.append(("cases", f"\n<b>📁 New case merged in the TLDP library</b>\n"
                                f'• <a href="{pr["html_url"]}">{esc(pr["title"])}</a> by {esc(pr["user"]["login"])}\n'
                                f'  <a href="https://github.com/{CASES_REPO}">browse all cases</a> · add yours: fork, copy the template, open a pull request'))
    parts = []
    for sector, (sector_label, orgs) in ORGS.items():
        try:
            found = gh_search(org_query(orgs), "updated")
            index_add(f"orgs|{sector}|{sector_label}", found)
            picks = pick(found, seen, 2)
        except BudgetExceeded:
            break
        except Exception as e:
            print(f"orgs/{sector}: {e}", file=sys.stderr)
            continue
        parts += [render_org(p, sector_label) for p in picks]
    new_hacks = []
    all_hacks = hackathons_nyc()
    _index["hackathons"] = {"at": datetime.now().isoformat(timespec="minutes"), "items": all_hacks}
    for h in all_hacks:
        if "hack:" + h["url"] not in seen:
            seen["hack:" + h["url"]] = date.today().isoformat()
            new_hacks.append(h)
    if new_hacks:
        chunks.append(("hackathons", "\n<b>🏁 NYC in-person hackathons — new listings</b>\n<i>Spring requirement: attend one. Register early, teams fill up.</i>\n" +
                       "\n".join(render_hack(h) for h in new_hacks[:8]) + hack_footer()))
    if parts:
        chunks.append(("orgs", "\n<b>🤝 Contribute to mission-driven orgs — resume-ready experience</b>\n" + "\n".join(parts) +
                       '\n  🔎 <a href="https://www.linkedin.com/jobs/search/?keywords=%22open%20source%22%20volunteer">'
                       "open-source volunteer roles on LinkedIn</a>"))
    return chunks


HEADER = ("<b>🧪 Project Scout — new on GitHub</b>\n"
          "Build it, contribute to it, or reproduce the research — steal the idea, make your own version.\n"
          f"<i>📶 {phase()[1]}. Lists run easiest → hardest.</i>")
FOOTER = "\n\n<i>/quant /fintech /swe /cyber /data /pm /marketing · /oss &lt;major&gt; · /research &lt;major&gt; · /orgs — live search anytime.</i>"


def targets() -> list[tuple[str, str]]:
    """(bot token, chat id) pairs: your private bot + optional campus bot -> public channel students join."""
    t = [(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])]
    if os.environ.get("CAMPUS_BOT_TOKEN") and os.environ.get("CAMPUS_CHAT_ID"):
        t.append((os.environ["CAMPUS_BOT_TOKEN"], os.environ["CAMPUS_CHAT_ID"]))
    return t


def send(text: str) -> None:
    for tok, chat in targets():
        body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:  # 400 "chat not found" = press Start / make bot channel admin; 404 = bad token
            print(f"telegram {e.code} for chat {chat}: {e.read()[:200].decode(errors='replace')}", file=sys.stderr)  # other targets still get it


def md_esc(s: str) -> str:
    """Discord markdown escape for third-party text: neutralises masked links, bold, code, quotes and @mentions."""
    import re
    return re.sub(r"([\\*_~`|<>\[\]()])", r"\\\1", s).replace("@", "@​")


def to_markdown(text: str) -> str:
    """Telegram HTML -> Discord markdown (<url> suppresses embeds). Our own tags become markdown; everything else —
    repo names, descriptions, hackathon titles — is markdown-escaped so it can't smuggle a link or a mention."""
    import re
    keep = []

    def stash(s: str) -> str:
        keep.append(s)
        return f"\x00{len(keep) - 1}\x00"
    unesc = lambda s: s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")  # noqa: E731
    text = re.sub(r'<a href="([^"]+)">([^<]*)</a>', lambda m: stash(f"[{md_esc(unesc(m.group(2)))}](<{m.group(1)}>)"), text)
    text = re.sub(r"</?b>", lambda m: stash("**"), text)
    text = re.sub(r"</?i>", lambda m: stash("*"), text)
    text = md_esc(unesc(text))  # entities decoded only after our own tags are stashed, so "&lt;i&gt;" can't become a tag
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def discord(key: str, text: str) -> None:
    """Optional Discord mirror. DISCORD_WEBHOOKS = JSON {major key or "orgs": webhook url} posts each section into its
    own channel (see discord_setup.py); DISCORD_WEBHOOK_URL posts everything into one channel."""
    hooks = json.loads(os.environ.get("DISCORD_WEBHOOKS") or "{}")
    url = hooks.get(key) or os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    md = to_markdown(text).strip()
    title = md.splitlines()[0].replace("*", "")[:60] + f" · {datetime.now():%b %d %H:%M}"
    thread = None  # feed channels are forums: the first part opens a post, later parts reply inside it
    for part in [md[i:i + 1900] for i in range(0, len(md), 1900)]:  # Discord cap is 2000 chars
        body = {"content": part}
        if thread is None:
            body["thread_name"] = title
        u = url + ("?wait=true" if thread is None else f"?wait=true&thread_id={thread}")
        req = urllib.request.Request(u, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "project-scout"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                thread = json.load(r).get("channel_id")
        except urllib.error.HTTPError as e:
            err = e.read()[:200].decode(errors="replace")
            if e.code == 400 and "thread_name" in err:  # plain text channel (single-channel DISCORD_WEBHOOK_URL)
                body.pop("thread_name", None)
                urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode(),
                                       headers={"Content-Type": "application/json", "User-Agent": "project-scout"}), timeout=30)
                thread = 0
            else:
                print(f"discord {e.code} for {key}: {err}", file=sys.stderr)
                return


def alert(text: str) -> None:
    """Ops alert to YOUR private bot only (never the campus channel / Discord)."""
    body = json.dumps({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}).encode()
    urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                                                  data=body, headers={"Content-Type": "application/json"}), timeout=30)


def messages(chunks: list[str], limit: int = 3900) -> list[str]:
    """Pack chunks into as few Telegram messages (<4096 chars) as possible."""
    msgs, cur = [], ""
    for c in chunks:
        if cur and len(cur) + len(c) + 1 > limit:
            msgs.append(cur)
            cur = ""
        cur = f"{cur}\n{c}" if cur else c
    return msgs + [cur] if cur else msgs


def self_check() -> None:
    assert messages(["a" * 3000, "b" * 3000, "c"]) == ["a" * 3000, "b" * 3000 + "\nc"]
    assert messages([]) == []
    assert to_markdown('<b>x</b> <a href="https://u">t</a> &lt;i&gt;') == "**x** [t](<https://u>) \\<i\\>"
    # a hostile repo description cannot smuggle a masked link, bold or a mention into Discord
    assert to_markdown('<a href="https://ok">[x](https://evil) @everyone</a> **bold** [y](https://evil)') \
        == "[\\[x\\]\\(https://evil\\) @​everyone](<https://ok>) \\*\\*bold\\*\\* \\[y\\]\\(https://evil\\)"
    fx = lambda n, d: {"full_name": n, "description": d, "language": "Python", "size": 500, "stargazers_count": 5}  # noqa: E731
    assert pick([fx("x/y", "seen"), fx("a/b", "fresh"), fx("c/d", "extra")], {"x/y": "2099-01-01"}, 1) == [fx("a/b", "fresh")]
    assert pick([fx("a/b", "")], {}, 1) == []  # English-only also means: must have a description
    assert not english("") and english("Agent-native backtesting") and not english("面向基本面因子研究的智能体-AI agent")
    r = {"full_name": "<b>", "html_url": "u", "stargazers_count": 1, "description": ""}
    assert "&lt;b&gt;" in render_repo(r, "build") and "good-first-issues" in render_repo(r, "oss")
    assert "good-first-issues:>0" in LANES["oss"][3]("t", "trading")
    assert LANES["research"][3]("t", "trading").startswith('"quantitative finance" trading arxiv')
    assert pick([fx("x/gpt-agent", "an LLM agent"), fx("y/scanner", "port scanner")], {}, 5, "cyber") == [fx("y/scanner", "port scanner")]
    assert difficulty({"size": 100, "stargazers_count": 10}) == "🟢 starter" and difficulty({"size": 99999, "stargazers_count": 10}) == "🔴 advanced"
    hard, easy = {"full_name": "h", "size": 99999, "stargazers_count": 9}, {"full_name": "e", "size": 10, "stargazers_count": 1}
    assert [r["full_name"] for r in ordered([hard, easy])] == ["e", "h"]
    assert phase(date(2026, 9, 15))[0] == 0 and phase(date(2026, 12, 1))[0] == 1 and phase(date(2027, 3, 1))[0] == 2
    hard2 = {"full_name": "m/m", "size": 20000, "stargazers_count": 900, "description": "mid", "language": "Go"}
    hard.update(description="hard", language="C", full_name="h/h"), easy.update(description="easy", language="Python", full_name="e/e", size=200)
    assert [r["full_name"] for r in pick([easy, hard2, hard], {}, 5, "swe", hard=True)] == ["h/h", "m/m"]  # hardest first, no starters
    assert "Challenge track" in render_path("swe", {})
    ok = {"full_name": "pandas-dev/pandas", "description": "Flexible data analysis", "language": "Python", "size": 400000, "stargazers_count": 49000}
    assert legit(ok)
    assert not legit({"full_name": "ElementTrail/Multichain-Drainer", "description": "A bot designed to steal assets from wallets", "language": None, "size": 581, "stargazers_count": 11})
    assert not legit({"full_name": "WildOctopusCrack/Ghostfolio-Privacy-First", "description": "wealth tracker", "language": None, "size": 12, "stargazers_count": 28})
    assert not legit({"full_name": "lightningfast66/forex-mt5-viper-strategy", "description": "trend-following strategy", "language": "MQL5", "size": 300, "stargazers_count": 10})
    assert not legit({"full_name": "sitimas9/cdcrafthh", "description": "account farm — OAuth + API key harvest", "language": "Python", "size": 6, "stargazers_count": 332})
    clones = [{"full_name": f"{o}/Ghostfolio-Dashboard", "description": "d", "language": "TypeScript", "size": 500, "stargazers_count": 28} for o in ("aa", "bb", "cc")]
    assert not legit(clones[0], clones)
    assert legit({"full_name": "trufflesecurity/trufflehog", "description": "Find, verify, and analyze leaked credentials", "language": "Go", "size": 52242, "stargazers_count": 27939})
    # GitHub search rejects a 6th AND/OR/NOT operator with a 422, so no query may carry more than five ORs
    queries = ([m[1] for m in MAJORS.values()] + [q for subs in BUILD_QUERIES.values() for _, q, _ in subs]
               + [q for cs in COURSES.values() for _, q in cs] + [d[1] for d in CYBER_DOMAINS.values()])
    assert max(q.count(" OR ") for q in queries) <= 5, max(queries, key=lambda q: q.count(" OR "))
    print("self-check ok")


def _arg_after(flag: str, default: str) -> str:
    i = sys.argv.index(flag)
    return sys.argv[i + 1] if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("-") else default


def main() -> int:
    if "--self-check" in sys.argv:
        self_check()
        return 0
    # ---- the gated pipeline (screen → publish); see pipeline.py ----
    if "--discover" in sys.argv:
        import pipeline
        return pipeline.discover(_arg_after("--out", "build"), full=False)
    if "--index-discover" in sys.argv:
        import pipeline
        return pipeline.discover(_arg_after("--out", "build"), full=True)
    if "--publish" in sys.argv:
        import pipeline
        return pipeline.publish(_arg_after("--publish", "build"), index_only=False)
    if "--publish-index" in sys.argv:
        import pipeline
        return pipeline.publish(_arg_after("--publish-index", "build"), index_only=True)
    if "--courses" in sys.argv:  # one-time evergreen map: top repos per course/topic, posted per major (optionally: --courses pm fintech)
        wanted = [a for a in sys.argv[sys.argv.index("--courses") + 1:] if a in COURSES] or list(COURSES)
        for key in wanted:
            courses, label = COURSES[key], MAJORS[key][0]
            rows = []
            for name, terms in courses:
                try:
                    items = ordered([r for r in gh_search(f"{terms} stars:>=50 archived:false", "stars", 8)
                                     if english(f'{r["full_name"]} {r.get("description") or ""}') and (r.get("description") or "").strip()])[:2]
                except Exception as e:
                    print(f"{key}/{name}: {e}", file=sys.stderr)
                    continue
                if items:
                    rows.append(f"<i>🎓 {esc(name)}</i>\n" + "\n".join(render_repo(r, "build") for r in items))
            head = (f"\n<b>🎓 {label} — course-aligned project ideas (Baruch)</b>\n"
                    "<i>Two well-known repos per course or topic, easiest first. Live search: /courses in the bot lists the codes.</i>")
            for m in messages([head] + rows):  # packs under Telegram's 4096-char limit across course rows
                send(m)
            if os.environ.get("DISCORD_WEBHOOKS"):
                discord(key, head + "\n" + "\n".join(rows))
            print(f"posted course map: {key} ({len(rows)} courses)")
        return 0
    if "--paths" in sys.argv:  # one-time: "your year, in order" per major (rerun after editing PATHS)
        for key, (label, *_r) in MAJORS.items():
            text = (f"\n<b>🗺 {label} — your year, in order</b>\n"
                    f"<i>Start at Stage 1 even if it feels easy; each stage assumes the one before. ▶ marks where we are now ({phase()[1]}).</i>"
                    + render_path(key, path_repos(key)) + "\n\n  💡 Type /path in the bot any time to see this again.")
            for m in messages([text]):
                send(m)
            discord(key, text)
        print("posted learning paths for all majors")
        return 0
    if "--projects" in sys.argv:  # one-time: the Python + SQL project ladder per CIS major (cyber, swe, data)
        wanted = [a for a in sys.argv[sys.argv.index("--projects") + 1:] if a in PROJECTS] or list(PROJECTS)
        for key in wanted:
            rows = []
            for level in ("start", "build", "deep"):
                items = [(e, gh_repo(e[2])) for e in PROJECTS[key] if e[0] == level]
                if items:
                    rows.append(f"<i>{LEVELS3[level]}</i>\n" + "\n".join(render_project(e, r) for e, r in items))
            text = (f"\n<b>🐍 {MAJORS[key][0]} — Python &amp; SQL projects, easiest first</b>\n"
                    "<i>Work down the list. Each one names what YOU build — finishing the task matters more than the repo.</i>\n"
                    + "\n".join(rows) + f"\n\n<i>ℹ️ {gate_label()}</i>")
            for m in messages([text]):
                send(m)
            discord(key, text)
            print(f"posted project ladder: {key} ({len(PROJECTS[key])} projects)")
        return 0
    if "--cases" in sys.argv:  # one-time: "case studies you can contribute to" per major
        for key, (label, *_r) in MAJORS.items():
            rows = [render_case(e, r) for e, r in cases_for(key)]
            text = (f"\n<b>📁 {label} — case studies you can contribute to</b>\n"
                    "<i>Real collections from legit orgs. Pick one, read its CONTRIBUTING file, claim an issue. Your own case goes in the TLDP library.</i>\n"
                    + "\n".join(rows))
            for m in messages([text]):
                send(m)
            discord(key, text)
        print("posted case-study sections for all majors")
        return 0
    if "--cyber-domains" in sys.argv:  # one-time: 8 curated posts (one per CISSP domain) into the cyber channel
        for k, (label, _t, repos, (ref_name, ref_url)) in CYBER_DOMAINS.items():
            items = ordered([r for r in (gh_repo(f) for f in repos) if r])
            text = (f"\n<b>🔐 {esc(label)} — legit projects to learn from and contribute to</b>\n" +
                    "\n".join(render_repo(r, "oss") for r in items) +
                    f'\n  📖 Reference: <a href="{ref_url}">{esc(ref_name)}</a>\n  💡 Live search: /cyber {k}')
            for m in messages([text]):
                send(m)
            discord("cyber", text)
        print("posted 8 cyber-domain sections")
        return 0
    if "--starters" in sys.argv:  # one-time evergreen post per major (Telegram + Discord); rerun after editing STARTERS
        chunks = starters()
        for m in messages([t for _, t in chunks]):
            send(m)
        for key, text in chunks:
            discord(key, text)
        print(f"posted {len(chunks)} start-here sections")
        return 0
    seen = load_seen()
    load_index()
    if "--index-only" in sys.argv:  # full rebuild of index.json (every lane, language, course, domain); sends nothing
        full_index()
        refresh_static()
        save_index()
        print(f"index: {sum(len(v) for v in _index['keys'].values())} rows in {len(_index['keys'])} keys · {_calls['n']} searches")
        return 0
    chunks = build_digest(seen)
    try:
        refresh_static()
    except Exception as e:
        print(f"static refresh: {e}", file=sys.stderr)
    save_index()
    msgs = messages([HEADER] + [t for _, t in chunks] + [FOOTER]) if chunks else []
    if "--preview" in sys.argv:
        print("\n\n=====\n\n".join(msgs) or "(nothing new)")
        return 0
    for m in msgs:
        send(m)
    for key, text in chunks:  # Discord: one post per section, into that section's channel
        discord(key, text)
    meta = seen.pop("_meta", {}) if isinstance(seen.get("_meta"), dict) else {}
    now = datetime.now()
    if chunks:
        meta["last_sent"] = now.isoformat(timespec="minutes")
    elif meta.get("last_sent") and now - datetime.fromisoformat(meta["last_sent"]) > timedelta(hours=48):
        alert(f"⚠️ Project Scout has posted nothing since {meta['last_sent']} — check the Actions log / GitHub search terms.")
    seen["_meta"] = meta
    json.dump(seen, open(SEEN, "w"), indent=0)
    status = rate_limit_status()
    print(f"sent {len(msgs)} message(s) at {datetime.now():%Y-%m-%d %H:%M} · {_calls['n']} GitHub searches · {status}")
    if _calls["limited"]:
        alert(f"⚠️ Project Scout hit GitHub's rate limit {_calls['limited']}× this run ({_calls['n']} searches). "
              f"It backed off and stopped early. If this repeats, lower SEARCH_BUDGET in project_scout.py. {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
