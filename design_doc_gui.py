#!/usr/bin/env python3
"""
설계 문서 자동 생성 도구
- ERD, 테이블 정의서, API 명세서, 클래스 다이어그램 자동 생성
- 규칙 기반 (무료) + API 키 있으면 AI 보완
"""
import os, re, json, threading, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ══════════════════════════════════════════════
#  파서 엔진
# ══════════════════════════════════════════════
SKIP_DIRS = {'target','build','.git','node_modules','__pycache__','.idea','out','dist','.gradle','.claude'}
ALLOWED_EXT = {'.java','.xml','.yml','.yaml','.properties','.sql','.json','.kt'}

def collect_files(src_dir, exts=None):
    files = []
    allow = exts or ALLOWED_EXT
    for root, dirs, fnames in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in fnames:
            if Path(f).suffix.lower() in allow:
                files.append(Path(root)/f)
    return sorted(files)

def read(fp):
    try: return fp.read_text(encoding='utf-8', errors='ignore')
    except: return ''

# ── 1. 테이블 정의서 (MyBatis XML + SQL 파싱) ─────────────
def extract_tables(files, src_dir):
    tables = {}

    # MyBatis XML에서 테이블 추출
    for fp in files:
        if fp.suffix.lower() != '.xml': continue
        content = read(fp)
        if 'mapper' not in content.lower() and 'mybatis' not in content.lower():
            if '<select' not in content and '<insert' not in content: continue

        rel = str(fp.relative_to(src_dir))

        # resultMap에서 컬럼 추출
        for rm in re.finditer(r'<resultMap[^>]+id=["\'](\w+)["\'][^>]*type=["\']([^"\']+)["\'][^>]*>(.*?)</resultMap>', content, re.DOTALL):
            rm_id, rm_type, rm_body = rm.groups()
            class_name = rm_type.split('.')[-1]
            cols = []
            for col in re.finditer(r'<(?:result|id)\s+[^>]*column=["\'](\w+)["\'][^>]*property=["\'](\w+)["\']', rm_body):
                cols.append({'column': col.group(1), 'property': col.group(2), 'type': 'VARCHAR', 'pk': col.group(0).startswith('<id')})
            if cols:
                tbl_name = camel_to_snake(class_name).upper()
                if tbl_name not in tables:
                    tables[tbl_name] = {'name': tbl_name, 'class': class_name, 'columns': [], 'source': rel}
                for c in cols:
                    if not any(x['column']==c['column'] for x in tables[tbl_name]['columns']):
                        tables[tbl_name]['columns'].append(c)

        # SQL CREATE TABLE
        for ct in re.finditer(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?(\w+)["`]?\s*\((.*?)\);', content, re.DOTALL|re.IGNORECASE):
            tbl_name, body = ct.groups()
            tbl_name = tbl_name.upper()
            cols = []
            for line in body.split(','):
                line = line.strip()
                m = re.match(r'["`]?(\w+)["`]?\s+([\w()]+)(.*)$', line)
                if m and not re.match(r'(PRIMARY|UNIQUE|KEY|INDEX|CONSTRAINT|FOREIGN)', line, re.I):
                    cname, ctype, rest = m.groups()
                    is_pk = 'PRIMARY KEY' in rest.upper() or 'PRIMARY KEY' in body.upper() and f'({cname})' in body
                    cols.append({'column':cname,'property':snake_to_camel(cname),'type':ctype.upper(),'pk':is_pk,'nullable':'NOT NULL' not in rest.upper(),'comment':''})
            if cols:
                tables[tbl_name] = {'name':tbl_name,'class':'','columns':cols,'source':rel}

    # SQL 파일
    for fp in files:
        if fp.suffix.lower() != '.sql': continue
        content = read(fp)
        rel = str(fp.relative_to(src_dir))
        for ct in re.finditer(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?(\w+)["`]?\s*\((.*?)\);', content, re.DOTALL|re.IGNORECASE):
            tbl_name, body = ct.groups()
            tbl_name = tbl_name.upper()
            if tbl_name in tables: continue
            cols = []
            for line in body.split('\n'):
                line = line.strip().rstrip(',')
                cm = re.search(r'COMMENT\s+["\']([^"\']+)["\']', line, re.I)
                comment = cm.group(1) if cm else ''
                m = re.match(r'["`]?(\w+)["`]?\s+([\w()]+)', line)
                if m and not re.match(r'(PRIMARY|UNIQUE|KEY|INDEX|CONSTRAINT|FOREIGN)', line, re.I):
                    cname, ctype = m.groups()
                    is_pk = 'PRIMARY KEY' in line.upper()
                    cols.append({'column':cname,'property':snake_to_camel(cname),'type':ctype.upper(),'pk':is_pk,'nullable':'NOT NULL' not in line.upper(),'comment':comment})
            if cols:
                tables[tbl_name] = {'name':tbl_name,'class':'','columns':cols,'source':rel}

    # Java Entity/VO 클래스
    for fp in files:
        if fp.suffix.lower() != '.java': continue
        content = read(fp)
        if not any(x in content for x in ['@Entity','@Table','@Column','VO','Vo','Entity']): continue
        rel = str(fp.relative_to(src_dir))
        cls_m = re.search(r'(?:public\s+)?class\s+(\w+)', content)
        if not cls_m: continue
        class_name = cls_m.group(1)
        tbl_m = re.search(r'@Table\s*\(\s*name\s*=\s*["\'](\w+)["\']', content)
        tbl_name = tbl_m.group(1).upper() if tbl_m else camel_to_snake(class_name).upper()
        if tbl_name in tables: continue
        cols = []
        for field in re.finditer(r'(?:@Column[^;]*?)?\s*(?:private|protected|public)\s+(\w+)\s+(\w+)\s*;', content):
            ftype, fname = field.groups()
            if fname in ('serialVersionUID','log','logger'): continue
            col_m = re.search(rf'@Column\([^)]*name\s*=\s*["\'](\w+)["\']', content[:field.start()+200])
            col_name = col_m.group(1).upper() if col_m else camel_to_snake(fname).upper()
            id_m = re.search(r'@Id\s*\n.*?' + re.escape(fname), content, re.DOTALL)
            cols.append({'column':col_name,'property':fname,'type':java_to_sql_type(ftype),'pk':bool(id_m),'nullable':True,'comment':''})
        if cols:
            tables[tbl_name] = {'name':tbl_name,'class':class_name,'columns':cols,'source':rel}

    return tables

# ── 2. API 명세서 (Controller 파싱) ───────────────────────
def extract_apis(files, src_dir):
    apis = []
    for fp in files:
        if fp.suffix.lower() != '.java': continue
        content = read(fp)
        if not any(x in content for x in ['@Controller','@RestController','@RequestMapping']): continue
        rel = str(fp.relative_to(src_dir))
        cls_m = re.search(r'(?:public\s+)?class\s+(\w+)', content)
        cls_name = cls_m.group(1) if cls_m else fp.stem
        base_m = re.search(r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']', content)
        base_path = base_m.group(1) if base_m else ''

        for method in re.finditer(
            r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)'
            r'\s*\(?\s*(?:value\s*=\s*)?["\']?([^"\')\n]*)["\']?\s*\)?\s*'
            r'(?:.*?\n)*?\s*(?:public|private|protected)\s+\S+\s+(\w+)\s*\(([^)]*)\)',
            content, re.MULTILINE):
            method_type, path, func_name, params = method.groups()
            http = {'GetMapping':'GET','PostMapping':'POST','PutMapping':'PUT',
                    'DeleteMapping':'DELETE','PatchMapping':'PATCH','RequestMapping':'ALL'}.get(method_type,'ALL')
            full_path = (base_path.rstrip('/') + '/' + path.strip().lstrip('/')).rstrip('/')
            if not full_path.startswith('/'): full_path = '/' + full_path

            param_list = []
            for p in params.split(','):
                p = p.strip()
                if not p: continue
                ann = re.search(r'@(RequestParam|PathVariable|RequestBody|ModelAttribute)\s*(?:\([^)]*\))?\s*(\w+)\s+(\w+)', p)
                if ann:
                    param_list.append({'annotation':ann.group(1),'type':ann.group(2),'name':ann.group(3)})
                else:
                    pm = re.search(r'(\w+)\s+(\w+)$', p)
                    if pm: param_list.append({'annotation':'','type':pm.group(1),'name':pm.group(2)})

            apis.append({'class':cls_name,'method':http,'path':full_path,
                        'function':func_name,'params':param_list,'source':rel})
    return apis

# ── 3. 클래스 다이어그램 (Java 클래스 관계) ───────────────
def extract_classes(files, src_dir):
    classes = []
    for fp in files:
        if fp.suffix.lower() != '.java': continue
        content = read(fp)
        rel = str(fp.relative_to(src_dir))
        pkg_m = re.search(r'package\s+([\w.]+)\s*;', content)
        pkg = pkg_m.group(1) if pkg_m else ''
        cls_m = re.search(r'(?:public\s+)?(?:(abstract|interface|enum)\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s]+))?', content)
        if not cls_m: continue
        kind = cls_m.group(1) or 'class'
        cls_name = cls_m.group(2)
        extends = cls_m.group(3) or ''
        implements = [x.strip() for x in (cls_m.group(4) or '').split(',') if x.strip()]
        imports = re.findall(r'import\s+([\w.]+)\s*;', content)
        annotations = re.findall(r'@(\w+)', content[:500])
        fields = []
        for f in re.finditer(r'(?:private|protected|public)\s+(\w+)\s+(\w+)\s*;', content):
            ftype, fname = f.groups()
            if fname not in ('serialVersionUID','log','logger'):
                fields.append({'type':ftype,'name':fname})
        methods = []
        for m in re.finditer(r'(?:public|private|protected)\s+(?:static\s+)?(\w+)\s+(\w+)\s*\([^)]*\)', content):
            rtype, mname = m.groups()
            if mname not in ('get','set','toString','hashCode','equals'):
                methods.append({'return':rtype,'name':mname})

        classes.append({'name':cls_name,'package':pkg,'kind':kind,'extends':extends,
                       'implements':implements,'imports':imports,'annotations':annotations,
                       'fields':fields[:8],'methods':methods[:8],'source':rel})
    return classes

# ── 유틸 ──────────────────────────────────────────────────
def camel_to_snake(name):
    s = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s).lower()

def snake_to_camel(name):
    parts = name.lower().split('_')
    return parts[0] + ''.join(x.title() for x in parts[1:])

def java_to_sql_type(jtype):
    m = {'String':'VARCHAR(255)','Long':'BIGINT','Integer':'INT','int':'INT',
         'long':'BIGINT','Double':'DOUBLE','Float':'FLOAT','Boolean':'BOOLEAN',
         'bool':'BOOLEAN','Date':'DATE','LocalDate':'DATE','LocalDateTime':'DATETIME',
         'BigDecimal':'DECIMAL(18,2)'}
    return m.get(jtype, 'VARCHAR(255)')

# ══════════════════════════════════════════════
#  HTML 리포트 생성
# ══════════════════════════════════════════════
def generate_report(tables, apis, classes, ai_result, src_dir, plan_file, output_path):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    # ── ERD HTML (Mermaid) ──
    erd_lines = ['erDiagram']
    for tname, tbl in list(tables.items())[:20]:
        erd_lines.append(f'  {tname} {{')
        for col in tbl['columns'][:10]:
            pk = 'PK' if col.get('pk') else ''
            erd_lines.append(f'    {col["type"].split("(")[0]} {col["column"]} {pk}')
        erd_lines.append('  }')
    erd_code = '\n'.join(erd_lines)

    # ── 테이블 정의서 HTML ──
    tbl_html = ''
    for tname, tbl in tables.items():
        cols_html = ''
        for i, col in enumerate(tbl['columns'], 1):
            pk_badge = '<span style="background:#dbeafe;color:#1e40af;padding:1px 6px;border-radius:99px;font-size:10px;font-weight:700">PK</span>' if col.get('pk') else ''
            cols_html += f'''<tr style="background:{'#f8fafc' if i%2==0 else 'white'}">
                <td style="padding:7px 10px;border-bottom:1px solid #e5e7eb;font-size:12px;text-align:center;color:#64748b">{i}</td>
                <td style="padding:7px 10px;border-bottom:1px solid #e5e7eb;font-size:12px;font-family:monospace;font-weight:600">{col["column"]} {pk_badge}</td>
                <td style="padding:7px 10px;border-bottom:1px solid #e5e7eb;font-size:12px;font-family:monospace;color:#7c3aed">{col.get("type","VARCHAR")}</td>
                <td style="padding:7px 10px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#64748b">{col.get("property","")}</td>
                <td style="padding:7px 10px;border-bottom:1px solid #e5e7eb;font-size:12px">{"O" if col.get("nullable",True) else "X"}</td>
                <td style="padding:7px 10px;border-bottom:1px solid #e5e7eb;font-size:12px;color:#94a3b8">{col.get("comment","")}</td>
            </tr>'''
        src_badge = f'<span style="font-size:10px;color:#94a3b8;font-family:monospace">{tbl.get("source","")}</span>'
        tbl_html += f'''
        <div style="margin-bottom:24px">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
            <span style="font-size:15px;font-weight:700;color:#1e3a5f">{tname}</span>
            {'<span style="font-size:11px;color:#6366f1;font-family:monospace">← '+tbl["class"]+'</span>' if tbl.get("class") else ''}
            {src_badge}
          </div>
          <table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
            <thead><tr style="background:#1e3a5f">
              <th style="padding:8px 10px;color:white;font-size:11px;width:40px">#</th>
              <th style="padding:8px 10px;color:white;font-size:11px;text-align:left">컬럼명</th>
              <th style="padding:8px 10px;color:white;font-size:11px;text-align:left">데이터타입</th>
              <th style="padding:8px 10px;color:white;font-size:11px;text-align:left">Java 필드</th>
              <th style="padding:8px 10px;color:white;font-size:11px">NULL허용</th>
              <th style="padding:8px 10px;color:white;font-size:11px;text-align:left">설명</th>
            </tr></thead>
            <tbody>{cols_html}</tbody>
          </table>
        </div>'''

    # ── API 명세서 HTML ──
    api_html = ''
    method_colors = {'GET':'#10b981','POST':'#3b82f6','PUT':'#f59e0b','DELETE':'#ef4444','PATCH':'#8b5cf6','ALL':'#64748b'}
    for api in apis:
        color = method_colors.get(api['method'], '#64748b')
        params_html = ''
        for p in api['params']:
            ann_color = {'RequestParam':'#dbeafe','PathVariable':'#fef3c7','RequestBody':'#dcfce7','ModelAttribute':'#f3e8ff'}.get(p['annotation'],'#f1f5f9')
            params_html += f'<span style="background:{ann_color};padding:2px 8px;border-radius:4px;font-size:11px;font-family:monospace;margin-right:4px">{p["annotation"]} {p["type"]} {p["name"]}</span>'
        api_html += f'''
        <div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;margin-bottom:10px;display:flex;align-items:flex-start;gap:12px">
          <span style="background:{color};color:white;padding:3px 10px;border-radius:6px;font-size:12px;font-weight:700;flex-shrink:0;min-width:60px;text-align:center">{api["method"]}</span>
          <div style="flex:1">
            <div style="font-family:monospace;font-size:13px;font-weight:600;color:#1e293b;margin-bottom:4px">{api["path"]}</div>
            <div style="font-size:11px;color:#64748b;margin-bottom:4px">{api["class"]}.{api["function"]}()</div>
            <div>{params_html}</div>
          </div>
          <span style="font-size:10px;color:#94a3b8;font-family:monospace">{api["source"].split(os.sep)[-1]}</span>
        </div>'''

    # ── 클래스 다이어그램 HTML ──
    cls_html = ''
    kind_colors = {'class':'#dbeafe','interface':'#dcfce7','abstract':'#fef3c7','enum':'#f3e8ff'}
    for cls in classes[:40]:
        color = kind_colors.get(cls['kind'],'#f1f5f9')
        fields_html = ''.join(f'<div style="font-size:11px;font-family:monospace;color:#475569;padding:2px 0">- {f["type"]} {f["name"]}</div>' for f in cls['fields'][:5])
        methods_html = ''.join(f'<div style="font-size:11px;font-family:monospace;color:#1e40af;padding:2px 0">+ {m["name"]}()</div>' for m in cls['methods'][:5])
        extends_html = f'<div style="font-size:10px;color:#94a3b8;margin-top:4px">extends {cls["extends"]}</div>' if cls['extends'] else ''
        impl_html = f'<div style="font-size:10px;color:#94a3b8">impl {", ".join(cls["implements"][:2])}</div>' if cls['implements'] else ''
        ann_html = ''.join(f'<span style="font-size:10px;color:#7c3aed">@{a} </span>' for a in cls['annotations'][:3])
        cls_html += f'''
        <div style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;min-width:180px;max-width:220px">
          <div style="background:{color};padding:8px 12px;border-bottom:1px solid #e5e7eb">
            <div style="font-size:10px;color:#64748b">{ann_html}</div>
            <div style="font-size:13px;font-weight:700;color:#1e293b">{cls["name"]}</div>
            <div style="font-size:10px;color:#64748b">{cls["package"].split(".")[-1] if cls["package"] else ""}</div>
            {extends_html}{impl_html}
          </div>
          <div style="padding:8px 12px;background:white">
            {fields_html}
            {'<div style="border-top:1px solid #e5e7eb;margin:4px 0"></div>' if fields_html and methods_html else ''}
            {methods_html}
          </div>
        </div>'''

    # ── AI 보완 결과 ──
    ai_html = ''
    if ai_result:
        ai_html = f'''
        <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:12px;padding:20px;margin-bottom:24px">
          <div style="font-size:14px;font-weight:700;color:#15803d;margin-bottom:12px">🤖 AI 분석 보완 결과</div>
          <div style="font-size:13px;color:#166534;line-height:1.8;white-space:pre-wrap">{ai_result}</div>
        </div>'''

    html = f'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>설계 문서 — {now}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Noto Sans KR",system-ui,sans-serif;background:#f8fafc;color:#1e293b}}
.hdr{{background:linear-gradient(135deg,#1e3a5f,#2563eb);color:white;padding:24px 40px}}
.hdr h1{{font-size:22px;font-weight:700}}.hdr p{{font-size:13px;opacity:.75;margin-top:4px}}
.nav{{display:flex;gap:0;background:white;border-bottom:2px solid #e5e7eb;padding:0 40px;position:sticky;top:0;z-index:100}}
.nav-btn{{padding:14px 20px;font-size:13px;font-weight:600;color:#64748b;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;background:none;border-top:none;border-left:none;border-right:none;font-family:inherit}}
.nav-btn.active{{color:#2563eb;border-bottom-color:#2563eb}}
.nav-btn:hover{{color:#1e3a5f}}
.body{{max-width:1400px;margin:0 auto;padding:28px 24px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:28px}}
.stat{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:14px;text-align:center}}
.num{{font-size:26px;font-weight:700;color:#2563eb;line-height:1;margin:4px 0}}
.lbl{{font-size:11px;color:#64748b}}
.section{{display:none}}.section.active{{display:block}}
.sec-title{{font-size:18px;font-weight:700;color:#1e3a5f;margin-bottom:20px;padding-bottom:10px;border-bottom:2px solid #e5e7eb}}
.cls-grid{{display:flex;flex-wrap:wrap;gap:12px}}
.mermaid-wrap{{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:24px;overflow-x:auto}}
.footer{{text-align:center;color:#94a3b8;font-size:12px;padding:24px}}
</style>
</head><body>
<div class="hdr">
  <h1>📐 설계 문서 자동 생성</h1>
  <p>소스: {src_dir} {'| 기획서: '+plan_file if plan_file else ''} | 생성: {now}</p>
</div>
<div class="nav">
  <button class="nav-btn active" onclick="show('overview')">개요</button>
  <button class="nav-btn" onclick="show('erd')">ERD ({len(tables)}개)</button>
  <button class="nav-btn" onclick="show('tables')">테이블 정의서 ({len(tables)}개)</button>
  <button class="nav-btn" onclick="show('apis')">API 명세서 ({len(apis)}개)</button>
  <button class="nav-btn" onclick="show('classes')">클래스 다이어그램 ({len(classes)}개)</button>
</div>
<div class="body">

<div id="overview" class="section active">
  <div class="stats">
    <div class="stat"><div class="lbl">테이블</div><div class="num">{len(tables)}</div><div class="lbl">개 추출</div></div>
    <div class="stat"><div class="lbl">API 엔드포인트</div><div class="num">{len(apis)}</div><div class="lbl">개 추출</div></div>
    <div class="stat"><div class="lbl">클래스</div><div class="num">{len(classes)}</div><div class="lbl">개 추출</div></div>
    <div class="stat"><div class="lbl">GET</div><div class="num" style="color:#10b981">{sum(1 for a in apis if a["method"]=="GET")}</div><div class="lbl">개</div></div>
    <div class="stat"><div class="lbl">POST</div><div class="num" style="color:#3b82f6">{sum(1 for a in apis if a["method"]=="POST")}</div><div class="lbl">개</div></div>
    <div class="stat"><div class="lbl">PUT/DELETE</div><div class="num" style="color:#f59e0b">{sum(1 for a in apis if a["method"] in ("PUT","DELETE"))}</div><div class="lbl">개</div></div>
  </div>
  {ai_html}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div style="background:white;border:1px solid #e5e7eb;border-radius:12px;padding:16px">
      <div style="font-size:14px;font-weight:700;color:#1e3a5f;margin-bottom:10px">📋 테이블 목록</div>
      {''.join(f'<div style="padding:5px 0;border-bottom:1px solid #f1f5f9;font-size:12px;font-family:monospace">{t}</div>' for t in list(tables.keys())[:20])}
    </div>
    <div style="background:white;border:1px solid #e5e7eb;border-radius:12px;padding:16px">
      <div style="font-size:14px;font-weight:700;color:#1e3a5f;margin-bottom:10px">🔗 API 목록</div>
      {''.join(f'<div style="padding:4px 0;border-bottom:1px solid #f1f5f9;font-size:11px"><span style="background:{method_colors.get(a["method"],"#64748b")};color:white;padding:1px 5px;border-radius:3px;font-size:10px;margin-right:6px">{a["method"]}</span>{a["path"]}</div>' for a in apis[:20])}
    </div>
  </div>
</div>

<div id="erd" class="section">
  <div class="sec-title">ERD (Entity Relationship Diagram)</div>
  <div class="mermaid-wrap">
    <div class="mermaid">{erd_code}</div>
  </div>
</div>

<div id="tables" class="section">
  <div class="sec-title">테이블 정의서</div>
  {tbl_html if tbl_html else '<div style="color:#94a3b8;padding:40px;text-align:center">MyBatis XML, SQL, Entity 클래스를 찾지 못했습니다</div>'}
</div>

<div id="apis" class="section">
  <div class="sec-title">API 명세서</div>
  {api_html if api_html else '<div style="color:#94a3b8;padding:40px;text-align:center">@Controller / @RestController 클래스를 찾지 못했습니다</div>'}
</div>

<div id="classes" class="section">
  <div class="sec-title">클래스 다이어그램</div>
  <div class="cls-grid">{cls_html}</div>
</div>

</div>
<div class="footer">설계 문서 자동 생성 도구 | {now}</div>
<script>
mermaid.initialize({{startOnLoad:true, theme:'default', securityLevel:'loose'}});
function show(id) {{
  document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}}
</script>
</body></html>'''

    Path(output_path).write_text(html, encoding='utf-8')

# ── Claude API 보완 ────────────────────────────────────────
def call_claude_api(api_key, tables, apis, classes, plan_text=''):
    try:
        summary = {
            'tables': list(tables.keys())[:10],
            'api_count': len(apis),
            'api_sample': [f'{a["method"]} {a["path"]}' for a in apis[:5]],
            'classes': [c['name'] for c in classes[:10]],
        }
        prompt = f"""아래는 Java 프로젝트 소스코드 분석 결과입니다.
{'기획서 내용:\n' + plan_text[:2000] if plan_text else ''}

분석 결과:
- 테이블: {summary['tables']}
- API 수: {summary['api_count']}개
- API 샘플: {summary['api_sample']}
- 주요 클래스: {summary['classes']}

다음 항목을 분석해 한국어로 작성해주세요:
1. 시스템 전체 구조 요약 (3줄)
2. 주요 테이블 간 관계 추정
3. 핵심 API 흐름 설명
4. 설계 보완이 필요한 항목
5. 추천 추가 산출물"""

        payload = json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 2000,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode('utf-8')

        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload,
            headers={'Content-Type':'application/json',
                     'x-api-key': api_key,
                     'anthropic-version': '2023-06-01'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data['content'][0]['text']
    except Exception as e:
        return f'AI 분석 실패: {e}'

# ══════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('설계 문서 자동 생성 도구')
        self.geometry('700x680')
        self.resizable(True, True)
        self.configure(bg='#f8fafc')
        self.running = False
        self._build()

    def _build(self):
        hdr = tk.Frame(self, bg='#1e3a5f', height=60)
        hdr.pack(fill='x')
        tk.Label(hdr, text='📐  설계 문서 자동 생성 도구', bg='#1e3a5f', fg='white',
                 font=('맑은 고딕',14,'bold')).pack(side='left', padx=20, pady=16)
        tk.Label(hdr, text='ERD · 테이블정의서 · API명세서 · 클래스다이어그램',
                 bg='#1e3a5f', fg='#93c5fd', font=('맑은 고딕',10)).pack(side='left', pady=16)

        body = tk.Frame(self, bg='#f8fafc', padx=24, pady=18)
        body.pack(fill='both', expand=True)

        # 소스 폴더
        self._label(body, '① 소스 폴더 선택')
        r1 = tk.Frame(body, bg='#f8fafc'); r1.pack(fill='x', pady=(4,10))
        self.src_var = tk.StringVar()
        tk.Entry(r1, textvariable=self.src_var, font=('맑은 고딕',10),
                 relief='solid', bd=1, bg='white').pack(side='left', fill='x', expand=True, ipady=6, padx=(0,8))
        tk.Button(r1, text='찾아보기', command=lambda: self.src_var.set(filedialog.askdirectory() or self.src_var.get()),
                  bg='#2563eb', fg='white', font=('맑은 고딕',10,'bold'),
                  relief='flat', padx=12, cursor='hand2').pack(side='left')

        # 기획서
        self._label(body, '② 기획서 파일 선택 (선택)')
        r2 = tk.Frame(body, bg='#f8fafc'); r2.pack(fill='x', pady=(4,10))
        self.plan_var = tk.StringVar()
        tk.Entry(r2, textvariable=self.plan_var, font=('맑은 고딕',10),
                 relief='solid', bd=1, bg='white').pack(side='left', fill='x', expand=True, ipady=6, padx=(0,8))
        tk.Button(r2, text='찾아보기',
                  command=lambda: self.plan_var.set(filedialog.askopenfilename(filetypes=[('HTML/텍스트','*.html *.htm *.txt *.md'),('전체','*.*')]) or self.plan_var.get()),
                  bg='#6366f1', fg='white', font=('맑은 고딕',10,'bold'),
                  relief='flat', padx=12, cursor='hand2').pack(side='left')

        # 저장 위치
        self._label(body, '③ 저장 위치')
        r3 = tk.Frame(body, bg='#f8fafc'); r3.pack(fill='x', pady=(4,10))
        self.out_var = tk.StringVar(value=str(Path.home()/'Desktop'))
        tk.Entry(r3, textvariable=self.out_var, font=('맑은 고딕',10),
                 relief='solid', bd=1, bg='white').pack(side='left', fill='x', expand=True, ipady=6, padx=(0,8))
        tk.Button(r3, text='찾아보기',
                  command=lambda: self.out_var.set(filedialog.askdirectory() or self.out_var.get()),
                  bg='#64748b', fg='white', font=('맑은 고딕',10,'bold'),
                  relief='flat', padx=12, cursor='hand2').pack(side='left')

        # API 키
        self._label(body, '④ Anthropic API 키 (선택 — 있으면 AI 분석 보완)')
        self.api_var = tk.StringVar()
        tk.Entry(body, textvariable=self.api_var, font=('맑은 고딕',10),
                 relief='solid', bd=1, bg='white', show='*').pack(fill='x', ipady=6, pady=(4,10))

        # 생성 항목 선택
        self._label(body, '⑤ 생성할 산출물')
        opt = tk.Frame(body, bg='#f8fafc'); opt.pack(fill='x', pady=(4,14))
        self.chk_erd   = tk.BooleanVar(value=True)
        self.chk_table = tk.BooleanVar(value=True)
        self.chk_api   = tk.BooleanVar(value=True)
        self.chk_cls   = tk.BooleanVar(value=True)
        for text, var in [('ERD',self.chk_erd),('테이블 정의서',self.chk_table),('API 명세서',self.chk_api),('클래스 다이어그램',self.chk_cls)]:
            tk.Checkbutton(opt, text=text, variable=var, bg='#f8fafc',
                          font=('맑은 고딕',10), cursor='hand2').pack(side='left', padx=(0,16))

        # 실행 버튼
        self.run_btn = tk.Button(body, text='📐  설계 문서 생성 시작', command=self._run,
                                 bg='#1e3a5f', fg='white', font=('맑은 고딕',13,'bold'),
                                 relief='flat', padx=20, pady=10, cursor='hand2')
        self.run_btn.pack(fill='x', pady=(0,12))

        # 진행바
        self.progress = ttk.Progressbar(body, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0,10))

        # 로그
        self._label(body, '실행 로그')
        self.log = scrolledtext.ScrolledText(body, height=10, font=('Consolas',9),
                                             bg='#0f172a', fg='#94a3b8',
                                             relief='flat', bd=0, state='disabled')
        self.log.pack(fill='both', expand=True, pady=(4,0))
        self.log.tag_config('ok',   foreground='#34d399')
        self.log.tag_config('err',  foreground='#f87171')
        self.log.tag_config('warn', foreground='#fbbf24')
        self.log.tag_config('info', foreground='#60a5fa')

    def _label(self, p, t):
        tk.Label(p, text=t, bg='#f8fafc', fg='#475569',
                 font=('맑은 고딕',10,'bold')).pack(anchor='w', pady=(0,2))

    def _log(self, msg, tag='info'):
        self.log.config(state='normal')
        ts = datetime.now().strftime('%H:%M:%S')
        self.log.insert('end', f'[{ts}] {msg}\n', tag)
        self.log.see('end')
        self.log.config(state='disabled')

    def _run(self):
        if self.running: return
        src = self.src_var.get().strip()
        if not src or not Path(src).exists():
            messagebox.showwarning('경고', '소스 폴더를 선택해주세요'); return
        self.running = True
        self.run_btn.config(state='disabled', text='생성 중...')
        self.progress.start(10)
        threading.Thread(target=self._generate, daemon=True).start()

    def _generate(self):
        try:
            src = Path(self.src_var.get())
            plan = self.plan_var.get().strip()
            out_dir = Path(self.out_var.get())
            api_key = self.api_var.get().strip()
            now_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            out_file = out_dir / f'design_doc_{now_str}.html'

            self._log(f'소스 폴더: {src}', 'info')

            self._log('[1/5] 파일 수집 중...', 'info')
            files = collect_files(src)
            self._log(f'      → {len(files)}개 파일', 'ok')

            tables, apis, classes = {}, [], []

            if self.chk_table.get() or self.chk_erd.get():
                self._log('[2/5] 테이블 구조 추출 중... (MyBatis·SQL·Entity)', 'info')
                tables = extract_tables(files, src)
                self._log(f'      → {len(tables)}개 테이블 추출', 'ok' if tables else 'warn')

            if self.chk_api.get():
                self._log('[3/5] API 엔드포인트 추출 중... (Controller)', 'info')
                apis = extract_apis(files, src)
                self._log(f'      → {len(apis)}개 API 추출', 'ok' if apis else 'warn')

            if self.chk_cls.get():
                self._log('[4/5] 클래스 구조 분석 중...', 'info')
                classes = extract_classes(files, src)
                self._log(f'      → {len(classes)}개 클래스 추출', 'ok' if classes else 'warn')

            ai_result = ''
            if api_key:
                self._log('[5/5] AI 분석 보완 중... (Claude API)', 'info')
                plan_text = ''
                if plan:
                    try:
                        p = TextExtractor(); p.feed(Path(plan).read_text(encoding='utf-8',errors='ignore'))
                        plan_text = '\n'.join(p.text)
                    except: pass
                ai_result = call_claude_api(api_key, tables, apis, classes, plan_text)
                self._log('      → AI 분석 완료', 'ok')
            else:
                self._log('[5/5] API 키 없음 — AI 보완 생략', 'warn')

            self._log('리포트 생성 중...', 'info')
            generate_report(tables, apis, classes, ai_result, str(src), plan, str(out_file))
            self._log(f'✅ 완료! → {out_file}', 'ok')

            import webbrowser
            webbrowser.open(out_file.as_uri())
            messagebox.showinfo('완료', f'설계 문서 생성 완료!\n\n{out_file}')

        except Exception as e:
            self._log(f'오류: {e}', 'err')
            messagebox.showerror('오류', str(e))
        finally:
            self.running = False
            self.run_btn.config(state='normal', text='📐  설계 문서 생성 시작')
            self.progress.stop()

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(); self.text=[]; self._skip=False
    def handle_starttag(self,tag,attrs):
        if tag in ('style','script'): self._skip=True
    def handle_endtag(self,tag):
        if tag in ('style','script'): self._skip=False
    def handle_data(self,data):
        if not self._skip:
            d=data.strip()
            if d: self.text.append(d)

if __name__ == '__main__':
    App().mainloop()
