#!/usr/bin/env python3
"""설계 문서 자동 생성 도구 v2 - 실제 설계서 형식 적용"""
import os, re, json, threading, urllib.request
from pathlib import Path
from datetime import datetime
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

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

def camel_to_snake(name):
    s = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s).lower()

def java_to_sql_type(jtype):
    m = {'String':'VARCHAR','Long':'BIGINT','Integer':'INT','int':'INT',
         'long':'BIGINT','Double':'DOUBLE','Float':'FLOAT','Boolean':'CHAR',
         'Date':'DATE','LocalDate':'DATE','LocalDateTime':'TIMESTAMP','BigDecimal':'NUMERIC'}
    return m.get(jtype,'VARCHAR')

# ── 테이블 파싱 ─────────────────────────────────────────
def extract_tables(files, src_dir):
    tables = {}

    for fp in files:
        if fp.suffix.lower() == '.sql':
            content = read(fp)
            for ct in re.finditer(
                r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?(\w+)["`]?\s*\((.*?)\)\s*;',
                content, re.DOTALL|re.IGNORECASE):
                tbl_name, body = ct.groups()
                tbl_name = tbl_name.upper()
                cols = []
                no = 1
                for line in body.split('\n'):
                    line = line.strip().rstrip(',')
                    if not line or re.match(r'(PRIMARY|UNIQUE|KEY|INDEX|CONSTRAINT|FOREIGN)', line, re.I):
                        continue
                    cm = re.search(r'COMMENT\s+["\']([^"\']+)["\']', line, re.I)
                    comment = cm.group(1) if cm else ''
                    m = re.match(r'["`]?(\w+)["`]?\s+([\w()]+)', line)
                    if m:
                        cname, ctype = m.groups()
                        length = ''
                        lm = re.search(r'\(([^)]+)\)', ctype)
                        if lm: length = lm.group(1); ctype = ctype[:ctype.index('(')]
                        is_pk = 'PRIMARY KEY' in line.upper()
                        is_nn = 'NOT NULL' in line.upper()
                        cols.append({'no':no,'id':cname,'name':comment or cname,
                                    'type':ctype.upper(),'length':length,
                                    'null':'NN' if is_nn else '','key':'PK' if is_pk else '',
                                    'default':'','remark':'','design':''})
                        no += 1
                if cols:
                    tables[tbl_name] = {'name':tbl_name,'korean':tbl_name,'desc':'',
                                        'columns':cols,'source':str(fp.relative_to(src_dir))}

    for fp in files:
        if fp.suffix.lower() != '.xml': continue
        content = read(fp)
        if not any(x in content for x in ['<resultMap','<select','<insert','mapper']): continue
        rel = str(fp.relative_to(src_dir))
        for rm in re.finditer(
            r'<resultMap[^>]+id=["\'](\w+)["\'][^>]*type=["\']([^"\']+)["\'][^>]*>(.*?)</resultMap>',
            content, re.DOTALL):
            rm_id, rm_type, rm_body = rm.groups()
            class_name = rm_type.split('.')[-1]
            tbl_name = camel_to_snake(class_name).upper()
            if tbl_name in tables: continue
            cols = []; no = 1
            for col in re.finditer(
                r'<(?:result|id)\s+[^>]*column=["\'](\w+)["\'][^>]*property=["\'](\w+)["\']',
                rm_body):
                cn, prop = col.groups()
                is_pk = col.group(0).startswith('<id')
                cols.append({'no':no,'id':cn,'name':cn,'type':'VARCHAR','length':'',
                             'null':'NN' if is_pk else '','key':'PK' if is_pk else '',
                             'default':'','remark':'','design':''})
                no += 1
            if cols:
                tables[tbl_name] = {'name':tbl_name,'korean':class_name,'desc':'',
                                    'columns':cols,'source':rel}

    for fp in files:
        if fp.suffix.lower() != '.java': continue
        content = read(fp)
        if not any(x in content for x in ['@Entity','@Table',]): continue
        rel = str(fp.relative_to(src_dir))
        cls_m = re.search(r'(?:public\s+)?class\s+(\w+)', content)
        if not cls_m: continue
        class_name = cls_m.group(1)
        tbl_m = re.search(r'@Table\s*\(\s*name\s*=\s*["\'](\w+)["\']', content)
        tbl_name = tbl_m.group(1).upper() if tbl_m else camel_to_snake(class_name).upper()
        if tbl_name in tables: continue
        cols = []; no = 1
        for field in re.finditer(
            r'(?:private|protected|public)\s+(\w+)\s+(\w+)\s*;', content):
            ftype, fname = field.groups()
            if fname in ('serialVersionUID','log','logger'): continue
            col_name = camel_to_snake(fname).upper()
            id_check = re.search(r'@Id\s*\n\s*.*?' + re.escape(fname), content[:field.start()+100], re.DOTALL)
            cols.append({'no':no,'id':col_name,'name':fname,'type':java_to_sql_type(ftype),'length':'',
                        'null':'NN' if id_check else '','key':'PK' if id_check else '',
                        'default':'','remark':'','design':''})
            no += 1
        if cols:
            tables[tbl_name] = {'name':tbl_name,'korean':class_name,'desc':'',
                                'columns':cols,'source':rel}
    return tables

# ── API 파싱 ─────────────────────────────────────────────
def extract_apis(files, src_dir):
    apis = []
    for fp in files:
        if fp.suffix.lower() != '.java': continue
        content = read(fp)
        if not any(x in content for x in ['@Controller','@RestController']): continue
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
            mtype, path, func, params_str = method.groups()
            http = {'GetMapping':'GET','PostMapping':'POST','PutMapping':'PUT',
                    'DeleteMapping':'DELETE','PatchMapping':'PATCH','RequestMapping':'ALL'}.get(mtype,'ALL')
            full = (base_path.rstrip('/')+'/'+path.strip().lstrip('/')).rstrip('/')
            if not full.startswith('/'): full = '/'+full
            params = []
            for p in params_str.split(','):
                p=p.strip()
                if not p: continue
                a = re.search(r'@(RequestParam|PathVariable|RequestBody|ModelAttribute)\s*(?:\([^)]*\))?\s*(\w+)\s+(\w+)',p)
                if a: params.append({'ann':a.group(1),'type':a.group(2),'name':a.group(3)})
                else:
                    pm = re.search(r'(\w+)\s+(\w+)$',p)
                    if pm: params.append({'ann':'','type':pm.group(1),'name':pm.group(2)})
            apis.append({'no':len(apis)+1,'class':cls_name,'method':http,'path':full,
                        'function':func,'params':params,'source':rel,'desc':''})
    return apis

# ── 클래스 파싱 ──────────────────────────────────────────
def extract_classes(files, src_dir):
    classes = []
    for fp in files:
        if fp.suffix.lower() != '.java': continue
        content = read(fp)
        rel = str(fp.relative_to(src_dir))
        pkg_m = re.search(r'package\s+([\w.]+)\s*;', content)
        pkg = pkg_m.group(1) if pkg_m else ''
        cls_m = re.search(r'(?:public\s+)?(?:(abstract|interface|enum)\s+)?class\s+(\w+)'
                          r'(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s]+))?', content)
        if not cls_m: continue
        kind = cls_m.group(1) or 'class'
        cls_name = cls_m.group(2)
        extends = cls_m.group(3) or ''
        implements = [x.strip() for x in (cls_m.group(4) or '').split(',') if x.strip()]
        anns = re.findall(r'@(\w+)', content[:500])
        fields = []
        for f in re.finditer(
            r'(private|protected|public)\s+(?:static\s+)?(?:final\s+)?(\w+)\s+(\w+)\s*(?:=\s*([^;]+))?;',
            content):
            acc, ft, fn, default = f.group(1), f.group(2), f.group(3), (f.group(4) or '').strip()
            if fn not in ('serialVersionUID','log','logger','INSTANCE'):
                fields.append({'access':acc,'type':ft,'name':fn,
                               'default':default[:30] if default else 'N/A','remark':''})
        methods = []
        for m in re.finditer(
            r'(public|private|protected)\s+(?:static\s+)?(\w+)\s+(\w+)\s*\(([^)]*)\)\s*(?:throws[^{]+)?\{',
            content):
            acc, rt, mn, params_raw = m.groups()
            if mn in ('main',): continue
            params = []
            for p in params_raw.split(','):
                p = re.sub(r'@\w+(?:\([^)]*\))?\s*', '', p).strip()
                pm = re.match(r'(?:final\s+)?([\w<>\[\].,\s]+)\s+(\w+)$', p.strip())
                if pm:
                    params.append(pm.group(1).strip())
            methods.append({'access':acc,'return':rt,'name':mn,'params':params,'remark':''})
        classes.append({'name':cls_name,'package':pkg,'kind':kind,'extends':extends,
                       'implements':implements,'annotations':anns,
                       'fields':fields[:15],'methods':methods[:15],'source':rel})
    return classes

def group_classes_by_domain(classes):
    """클래스를 도메인별로 그룹핑 (Controller+Service+Mapper 세트)"""
    ROLE_MAP = [
        ('controller', ['RestController','Controller']),
        ('service',    ['ServiceImpl','Service']),
        ('mapper',     ['MapperImpl','Mapper','MDAO','DAOImpl','DAO','Dao','RepositoryImpl','Repository']),
        ('component',  ['Component','Config','Util','Helper','Filter','Interceptor','Aspect','Handler']),
        ('model',      ['VO','DTO','Request','Response','Entity','Model','Form']),
    ]

    def get_domain_role(name):
        for role, suffixes in ROLE_MAP:
            for sfx in sorted(suffixes, key=len, reverse=True):
                if name.endswith(sfx) and len(name) > len(sfx):
                    return name[:-len(sfx)], role
        return name, 'class'

    groups = {}
    for cls in classes:
        domain, role = get_domain_role(cls['name'])
        if domain not in groups:
            groups[domain] = []
        groups[domain].append({**cls, 'role': role})

    result = []
    for idx, (domain, cls_list) in enumerate(sorted(groups.items()), 1):
        result.append({'id': f'DC-{idx:03d}', 'domain': domain, 'classes': cls_list})
    return result

# ── 인터페이스 파싱 ──────────────────────────────────────
def extract_interfaces(files, src_dir):
    """외부 연계/인터페이스 항목 추출"""
    ifaces = []
    no = 1

    for fp in files:
        rel = str(fp.relative_to(src_dir))
        content = read(fp)

        if fp.suffix.lower() == '.java':
            # @FeignClient
            fc = re.search(
                r'@FeignClient\s*\([^)]*(?:name|value)\s*=\s*["\']([^"\']+)["\']', content)
            if fc:
                methods = re.findall(
                    r'@(?:Get|Post|Put|Delete|Patch)Mapping\s*(?:\([^)]*\))?\s*\n\s*\w+\s+(\w+)\s*\(',
                    content)
                items = [{'name': m, 'type': 'method'} for m in methods[:10]]
                ifaces.append({'no': f'IA_{no:03d}', 'name': fc.group(1),
                               'id': fp.stem, 'method': 'Online/API',
                               'type': 'FeignClient', 'items': items, 'source': rel})
                no += 1

            # RestTemplate / WebClient calls
            rest_urls = re.findall(
                r'(?:exchange|getFor\w+|postFor\w+|put|delete)\s*\(\s*["\']([^"\']{5,100})["\']',
                content)
            if rest_urls:
                cls_m = re.search(r'class\s+(\w+)', content)
                name = cls_m.group(1) if cls_m else fp.stem
                items = [{'name': u, 'type': 'url'} for u in rest_urls[:10]]
                ifaces.append({'no': f'IA_{no:03d}', 'name': name,
                               'id': fp.stem, 'method': 'Online/REST',
                               'type': 'RestTemplate', 'items': items, 'source': rel})
                no += 1

        elif fp.suffix.lower() in ('.yml', '.yaml', '.properties'):
            # URL/endpoint 설정값
            urls = re.findall(
                r'(?:url|endpoint|api[-_]url|base[-_]url|host)\s*[:=]\s*(https?://[^\s\n#]+)',
                content, re.I)
            if urls:
                items = [{'name': u, 'type': 'config'} for u in urls[:10]]
                ifaces.append({'no': f'IA_{no:03d}', 'name': fp.name,
                               'id': fp.stem, 'method': 'Config',
                               'type': 'Properties', 'items': items, 'source': rel})
                no += 1

    return ifaces

# ── AI 보완 ──────────────────────────────────────────────
def call_claude_api(api_key, tables, apis, classes, plan_text=''):
    try:
        tbl_names = list(tables.keys())[:15]
        api_sample = [a['method']+' '+a['path'] for a in apis[:8]]
        cls_names = [c['name'] for c in classes[:15]]
        plan_part = ('기획서 내용:\n' + plan_text[:3000]) if plan_text else ''
        prompt = (
            '아래는 Java 프로젝트 소스코드 분석 결과입니다.\n'
            + plan_part + '\n\n'
            + '분석 결과:\n'
            + '- 테이블: ' + str(tbl_names) + '\n'
            + '- API 수: ' + str(len(apis)) + '개\n'
            + '- API 샘플: ' + str(api_sample) + '\n'
            + '- 주요 클래스: ' + str(cls_names) + '\n\n'
            + '다음 항목을 한국어로 작성해주세요:\n'
            + '1. 시스템 전체 구조 요약 (3줄)\n'
            + '2. 주요 테이블 간 관계 및 한글명 추정\n'
            + '3. 테이블별 설명 보완 (테이블명: 설명 형식으로)\n'
            + '4. 설계 보완 필요 항목\n'
            + '5. 추천 추가 산출물'
        )
        payload = json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 2000,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages', data=payload,
            headers={'Content-Type':'application/json','x-api-key':api_key,
                     'anthropic-version':'2023-06-01'}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())['content'][0]['text']
    except Exception as e:
        return f'AI 분석 실패: {e}'

# ── ERD SVG 생성 (Mermaid 없이 직접) ─────────────────────
def generate_erd_svg(tables):
    if not tables: return '<div style="color:#94a3b8;padding:40px;text-align:center">테이블 정보 없음</div>'
    items = list(tables.items())[:16]
    cols_per_row = 4
    box_w, box_h_base = 200, 32
    pad = 24
    rows = []
    for i in range(0, len(items), cols_per_row):
        rows.append(items[i:i+cols_per_row])

    svg_rows = []
    y = 20
    total_height = 20
    for row in rows:
        row_h = 0
        for j, (tname, tbl) in enumerate(row):
            col_count = len(tbl['columns'])
            h = box_h_base + col_count * 22 + 16
            row_h = max(row_h, h)
        for j, (tname, tbl) in enumerate(row):
            x = 20 + j * (box_w + pad)
            col_count = len(tbl['columns'])
            h = box_h_base + col_count * 22 + 16
            svg_rows.append(f'<rect x="{x}" y="{y}" width="{box_w}" height="{h}" rx="6" fill="#EEF2FF" stroke="#6366f1" stroke-width="1"/>')
            svg_rows.append(f'<rect x="{x}" y="{y}" width="{box_w}" height="28" rx="6" fill="#4F46E5"/>')
            svg_rows.append(f'<rect x="{x}" y="{y+22}" width="{box_w}" height="6" fill="#4F46E5"/>')
            svg_rows.append(f'<text x="{x+box_w//2}" y="{y+18}" text-anchor="middle" fill="white" font-size="11" font-weight="bold" font-family="맑은 고딕,Arial">{tname[:22]}</text>')
            if tbl.get('korean') and tbl['korean'] != tname:
                korean_short = tbl['korean'][:18]
            else:
                korean_short = ''
            cy = y + 44
            for col in tbl['columns'][:col_count]:
                pk = col.get('key','')=='PK'
                nn = col.get('null','')=='NN'
                icon = '🔑' if pk else ('●' if nn else '○')
                col_id = str(col.get('id',''))[:20]
                col_type = str(col.get('type',''))[:10]
                fill = '#4F46E5' if pk else '#374151'
                svg_rows.append(f'<text x="{x+8}" y="{cy}" fill="{fill}" font-size="10" font-family="맑은 고딕,Arial">{icon} {col_id}</text>')
                svg_rows.append(f'<text x="{x+box_w-8}" y="{cy}" text-anchor="end" fill="#6B7280" font-size="9" font-family="Arial">{col_type}</text>')
                cy += 22
        y += row_h + pad
        total_height = y

    total_w = min(len(rows[0]) if rows else 1, cols_per_row) * (box_w + pad) + 20
    svg = f'<svg width="100%" viewBox="0 0 {total_w} {total_height+20}" xmlns="http://www.w3.org/2000/svg">'
    svg += ''.join(svg_rows)
    svg += '</svg>'
    return svg

# ── HTML 리포트 생성 (실제 설계서 형식) ──────────────────
def generate_report(tables, apis, classes, ai_result, src_dir, plan_file, output_path, files=None):
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    date_str = datetime.now().strftime('%Y. %m. %d')

    # ── 테이블 정의서 (실제 설계서 형식) ──
    tbl_sections = ''
    for tname, tbl in tables.items():
        cols_html = ''
        for col in tbl['columns']:
            pk_style = 'font-weight:700;color:#1e3a5f;' if col.get('key')=='PK' else ''
            cols_html += f'''<tr>
              <td style="text-align:center;width:40px">{col.get('no','')}</td>
              <td style="font-family:monospace;font-size:11px;{pk_style}">{col.get('id','')}
                {'<span style="background:#dbeafe;color:#1e40af;font-size:9px;padding:1px 4px;border-radius:3px;margin-left:4px">PK</span>' if col.get('key')=='PK' else ''}
              </td>
              <td>{col.get('name','')}</td>
              <td style="font-family:monospace;font-size:11px;color:#7c3aed">{col.get('type','')}</td>
              <td style="text-align:center">{col.get('length','')}</td>
              <td style="text-align:center;color:#dc2626">{col.get('null','')}</td>
              <td style="text-align:center">{col.get('default','')}</td>
              <td style="color:#64748b;font-size:11px">{col.get('remark','')}</td>
              <td style="text-align:center;font-size:11px;color:#0369a1">{col.get('design','')}</td>
            </tr>'''

        tbl_sections += f'''
        <div class="tbl-block">
          <table class="tbl-meta">
            <tr><td class="meta-label">테이블명</td><td class="meta-val mono">{tname}</td>
                <td class="meta-label">한글명</td><td class="meta-val">{tbl.get('korean','')}</td></tr>
            <tr><td class="meta-label">테이블설명</td><td class="meta-val" colspan="3">{tbl.get('desc','')}</td></tr>
            <tr><td class="meta-label">소스</td><td class="meta-val mono" colspan="3" style="font-size:10px;color:#64748b">{tbl.get('source','')}</td></tr>
          </table>
          <table class="col-table">
            <thead><tr>
              <th style="width:40px">NO</th><th style="width:160px">컬럼ID</th>
              <th style="width:120px">컬럼명</th><th style="width:90px">타입</th>
              <th style="width:60px">길이</th><th style="width:40px">NULL</th>
              <th style="width:80px">기본값</th><th>비고</th><th style="width:50px">설계구분</th>
            </tr></thead>
            <tbody>{cols_html}</tbody>
          </table>
        </div>'''

    # ── API 명세서 (실제 설계서 형식) ──
    METHOD_COLOR = {'GET':'#059669','POST':'#2563eb','PUT':'#d97706',
                    'DELETE':'#dc2626','PATCH':'#7c3aed','ALL':'#64748b'}
    api_sections = ''
    for api in apis:
        mc = METHOD_COLOR.get(api['method'],'#64748b')
        params_html = ''
        for p in api['params']:
            ann_bg = {'RequestParam':'#dbeafe','PathVariable':'#fef3c7',
                      'RequestBody':'#dcfce7','ModelAttribute':'#f3e8ff'}.get(p.get('ann',''),'#f1f5f9')
            params_html += f'<span style="background:{ann_bg};padding:2px 7px;border-radius:4px;font-size:11px;font-family:monospace;margin:2px">{p.get("ann","")} {p.get("type","")} {p.get("name","")}</span>'
        api_sections += f'''
        <div class="api-block">
          <div class="api-head">
            <span class="method-badge" style="background:{mc}">{api['method']}</span>
            <span class="api-path">{api['path']}</span>
            <span class="api-func">{api['class']}.{api['function']}()</span>
          </div>
          <div class="api-params">{params_html or '<span style="color:#94a3b8;font-size:11px">파라미터 없음</span>'}</div>
        </div>'''

    # ── 클래스설계서 (PDF 형식 적용) ──
    ROLE_LABEL = {'controller':'Controller','service':'Service','mapper':'Mapper/MDAO',
                  'component':'Component','model':'VO/DTO','class':'Class'}
    KIND_COLOR = {'class':'#dbeafe','interface':'#dcfce7','abstract':'#fef3c7','enum':'#f3e8ff'}
    cls_design_sections = ''
    cls_groups = group_classes_by_domain(classes)
    for grp in cls_groups:
        # 클래스 구성 표
        comp_rows = ''
        for sub_no, cls in enumerate(grp['classes'], 1):
            sub_id = f"{grp['id']}.{sub_no:02d}"
            role = ROLE_LABEL.get(cls.get('role','class'), cls.get('role',''))
            comp_rows += f'<tr><td style="text-align:center">{sub_id}</td><td style="text-align:center">{role}</td><td style="font-family:monospace;font-weight:600">{cls["name"]}</td><td style="color:#64748b">{cls["package"]}</td></tr>'
        # 클래스 상세설계 (멤버변수 + 메서드)
        detail_blocks = ''
        for cls in grp['classes']:
            kc = KIND_COLOR.get(cls['kind'],'#f1f5f9')
            kind_label = {'class':'클래스','interface':'인터페이스','abstract':'추상클래스','enum':'열거형'}.get(cls['kind'],cls['kind'])
            ann_text = ' '.join(f'@{a}' for a in cls['annotations'][:4])
            ext_text = f'extends {cls["extends"]}' if cls['extends'] else ''
            impl_text = f'implements {", ".join(cls["implements"][:3])}' if cls['implements'] else ''
            # 멤버변수 행
            var_rows = ''.join(
                f'<tr><td style="font-family:monospace">{f["name"]}</td>'
                f'<td style="text-align:center">{f["access"]}</td>'
                f'<td style="font-family:monospace;color:#7c3aed">{f["type"]}</td>'
                f'<td style="color:#64748b">{f["default"]}</td>'
                f'<td>{f["remark"]}</td></tr>'
                for f in cls['fields']
            ) or '<tr><td colspan="5" style="color:#94a3b8;text-align:center">멤버변수 없음</td></tr>'
            # 메서드 행
            method_rows = ''.join(
                f'<tr><td style="font-family:monospace;font-weight:600;color:#1d4ed8">{m["name"]}</td>'
                f'<td style="text-align:center">{m["access"]}</td>'
                f'<td style="font-family:monospace;font-size:11px">{", ".join(m["params"][:5]) if m["params"] else "-"}</td>'
                f'<td style="font-family:monospace;color:#059669">{m["return"]}</td>'
                f'<td>{m["remark"]}</td></tr>'
                for m in cls['methods']
            ) or '<tr><td colspan="5" style="color:#94a3b8;text-align:center">메서드 없음</td></tr>'
            detail_blocks += f'''
            <div class="cls-detail-block">
              <table class="cls-detail-meta">
                <tr>
                  <td class="meta-label">클래스 ID</td><td class="meta-val mono">{cls["name"]}</td>
                  <td class="meta-label">패키지</td><td class="meta-val mono" style="font-size:10px">{cls["package"]}</td>
                </tr>
                <tr>
                  <td class="meta-label">종류</td><td class="meta-val">{kind_label} <span style="color:#7c3aed;font-size:11px">{ann_text}</span></td>
                  <td class="meta-label">상속/구현</td><td class="meta-val" style="font-size:11px">{ext_text} {impl_text}</td>
                </tr>
                <tr><td class="meta-label">소스</td><td class="meta-val mono" colspan="3" style="font-size:10px;color:#64748b">{cls["source"]}</td></tr>
              </table>
              <div class="cls-subsec-title">멤버변수</div>
              <table class="cls-var-tbl">
                <thead><tr><th>변수명</th><th style="width:80px">접근제어자</th><th style="width:130px">타입</th><th style="width:120px">초기값</th><th>비고</th></tr></thead>
                <tbody>{var_rows}</tbody>
              </table>
              <div class="cls-subsec-title" style="margin-top:8px">메서드</div>
              <table class="cls-var-tbl">
                <thead><tr><th>메서드명</th><th style="width:80px">접근제어자</th><th>파라미터(타입)</th><th style="width:100px">리턴타입</th><th>비고</th></tr></thead>
                <tbody>{method_rows}</tbody>
              </table>
            </div>'''
        cls_design_sections += f'''
        <div class="cls-domain-block">
          <div class="cls-domain-title">{grp["id"]} — {grp["domain"]}</div>
          <div class="cls-subsec-label">클래스 구성</div>
          <table class="cls-comp-tbl">
            <thead><tr><th style="width:100px">클래스 ID</th><th style="width:100px">분류</th><th style="width:200px">클래스명</th><th>패키지</th></tr></thead>
            <tbody>{comp_rows}</tbody>
          </table>
          <div class="cls-subsec-label" style="margin-top:12px">클래스 상세설계</div>
          {detail_blocks}
        </div>'''

    # ── 인터페이스설계서 (PDF 형식 적용) ──
    ifaces = extract_interfaces(files, Path(src_dir)) if files else []
    IF_TYPE_COLOR = {'FeignClient':'#dbeafe','RestTemplate':'#dcfce7','Properties':'#fef3c7','Config':'#fef3c7'}
    # IF 목록 표
    if_list_rows = ''
    for ia in ifaces:
        tc = IF_TYPE_COLOR.get(ia['type'],'#f1f5f9')
        if_list_rows += f'''<tr>
          <td style="text-align:center;font-weight:700;color:#1d4ed8">{ia["no"]}</td>
          <td style="font-family:monospace;font-weight:600">{ia["name"]}</td>
          <td style="font-family:monospace;font-size:11px">{ia["id"]}</td>
          <td style="text-align:center"><span style="background:{tc};padding:2px 7px;border-radius:4px;font-size:11px">{ia["type"]}</span></td>
          <td style="text-align:center">{ia["method"]}</td>
          <td style="font-size:10px;color:#64748b">{ia["source"]}</td>
        </tr>'''
    # IF 상세
    if_detail_sections = ''
    for ia in ifaces:
        items_html = ''
        for item in ia['items']:
            label = item.get('name','')
            itype = item.get('type','')
            items_html += f'<tr><td style="font-family:monospace">{label}</td><td style="color:#64748b">{itype}</td></tr>'
        if_detail_sections += f'''
        <div class="if-detail-block">
          <div class="if-detail-title">{ia["no"]} — {ia["name"]}</div>
          <table class="cls-detail-meta">
            <tr>
              <td class="meta-label">IF No.</td><td class="meta-val">{ia["no"]}</td>
              <td class="meta-label">시스템 ID</td><td class="meta-val mono">{ia["id"]}</td>
            </tr>
            <tr>
              <td class="meta-label">IF방식</td><td class="meta-val">{ia["method"]}</td>
              <td class="meta-label">유형</td><td class="meta-val">{ia["type"]}</td>
            </tr>
            <tr><td class="meta-label">소스</td><td class="meta-val mono" colspan="3" style="font-size:10px;color:#64748b">{ia["source"]}</td></tr>
          </table>
          {'<table class="cls-var-tbl" style="margin-top:8px"><thead><tr><th>연계 항목</th><th style="width:100px">유형</th></tr></thead><tbody>' + items_html + '</tbody></table>' if items_html else ''}
        </div>'''

    # ── ERD SVG ──
    erd_svg = generate_erd_svg(tables)

    # ── AI 결과 ──
    ai_html = ''
    if ai_result:
        ai_html = f'''<div class="ai-box">
          <div class="ai-title">🤖 AI 분석 보완</div>
          <div class="ai-body">{ai_result}</div>
        </div>'''

    ifaces_section_html = (
        '<div class="tbl-block">'
        '<div class="cls-domain-title">인터페이스 목록</div>'
        '<table class="if-list-tbl">'
        '<thead><tr><th style="width:80px">IF No.</th><th>인터페이스명</th>'
        '<th style="width:150px">시스템 ID</th><th style="width:100px">유형</th>'
        '<th style="width:90px">IF방식</th><th>소스</th></tr></thead>'
        '<tbody>' + if_list_rows + '</tbody>'
        '</table></div>'
        '<div class="sec-title" style="margin-top:24px">인터페이스 상세</div>'
        + if_detail_sections
    ) if ifaces else '<div style="color:#94a3b8;padding:40px;text-align:center">인터페이스 정보를 찾지 못했습니다</div>'

    html = f'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>설계 문서 — {now}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"맑은 고딕","Noto Sans KR",Arial,sans-serif;background:#f1f5f9;color:#1e293b;font-size:13px}}
/* 헤더 */
.hdr{{background:linear-gradient(135deg,#1e3a5f 0%,#1d4ed8 100%);color:white;padding:20px 36px;display:flex;align-items:center;gap:20px}}
.hdr-icon{{font-size:32px}}
.hdr h1{{font-size:20px;font-weight:700;margin-bottom:2px}}
.hdr p{{font-size:11px;opacity:.75}}
/* 네비 */
.nav{{display:flex;background:white;border-bottom:2px solid #e2e8f0;padding:0 36px;position:sticky;top:0;z-index:100;box-shadow:0 2px 4px rgba(0,0,0,.06)}}
.nav-btn{{padding:13px 18px;font-size:12px;font-weight:700;color:#64748b;cursor:pointer;border:none;border-bottom:3px solid transparent;margin-bottom:-2px;background:none;font-family:inherit;transition:all .15s}}
.nav-btn:hover{{color:#1e3a5f}}
.nav-btn.active{{color:#1d4ed8;border-bottom-color:#1d4ed8}}
/* 바디 */
.body{{max-width:1300px;margin:0 auto;padding:24px 20px}}
.section{{display:none}}.section.active{{display:block}}
.sec-title{{font-size:16px;font-weight:700;color:#1e3a5f;margin-bottom:6px;padding-bottom:8px;border-bottom:2px solid #1e3a5f;display:flex;align-items:center;gap:8px}}
.sec-sub{{font-size:11px;color:#64748b;margin-bottom:20px}}
/* 요약 카드 */
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:24px}}
.stat{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:14px;text-align:center;border-top:3px solid #1d4ed8}}
.stat-num{{font-size:28px;font-weight:700;color:#1d4ed8;line-height:1;margin:4px 0}}
.stat-lbl{{font-size:11px;color:#64748b}}
/* 테이블 정의서 */
.tbl-block{{background:white;border:1px solid #e2e8f0;border-radius:10px;margin-bottom:24px;overflow:hidden}}
.tbl-meta{{width:100%;border-collapse:collapse;background:#f8fafc;border-bottom:1px solid #e2e8f0}}
.tbl-meta td{{padding:7px 12px;font-size:12px;border:1px solid #e2e8f0}}
.meta-label{{background:#1e3a5f;color:white;font-weight:700;width:90px;text-align:center;font-size:11px}}
.meta-val{{color:#1e293b}}.meta-val.mono{{font-family:monospace;font-size:11px}}
.col-table{{width:100%;border-collapse:collapse;font-size:12px}}
.col-table th{{background:#334155;color:white;padding:7px 10px;text-align:left;font-size:11px;font-weight:600}}
.col-table td{{padding:7px 10px;border-bottom:1px solid #f1f5f9;vertical-align:middle}}
.col-table tr:nth-child(even) td{{background:#f8fafc}}
.col-table tr:hover td{{background:#eff6ff}}
/* API */
.api-block{{background:white;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;margin-bottom:8px;display:flex;flex-direction:column;gap:6px}}
.api-head{{display:flex;align-items:center;gap:10px}}
.method-badge{{color:white;padding:3px 10px;border-radius:5px;font-size:11px;font-weight:700;min-width:56px;text-align:center;flex-shrink:0}}
.api-path{{font-family:monospace;font-size:13px;font-weight:600;color:#1e293b}}
.api-func{{font-size:11px;color:#64748b;margin-left:auto}}
.api-params{{display:flex;flex-wrap:wrap;gap:4px}}
/* 클래스설계서 */
.cls-domain-block{{background:white;border:1px solid #e2e8f0;border-radius:10px;margin-bottom:28px;overflow:hidden}}
.cls-domain-title{{background:linear-gradient(90deg,#1e3a5f,#2563eb);color:white;padding:10px 18px;font-size:14px;font-weight:700;letter-spacing:.3px}}
.cls-subsec-label{{background:#f1f5f9;color:#1e3a5f;font-weight:700;font-size:12px;padding:7px 16px;border-bottom:1px solid #e2e8f0;border-top:1px solid #e2e8f0}}
.cls-comp-tbl{{width:100%;border-collapse:collapse;font-size:12px}}
.cls-comp-tbl th{{background:#334155;color:white;padding:7px 12px;text-align:left;font-size:11px}}
.cls-comp-tbl td{{padding:7px 12px;border-bottom:1px solid #f1f5f9}}
.cls-comp-tbl tr:hover td{{background:#f8fafc}}
.cls-detail-block{{border-top:1px solid #e2e8f0;padding:14px 16px 16px}}
.cls-detail-meta{{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:10px}}
.cls-detail-meta td{{padding:5px 10px;border:1px solid #e2e8f0}}
.cls-subsec-title{{font-size:11px;font-weight:700;color:#475569;background:#f8fafc;padding:4px 10px;border-left:3px solid #6366f1;margin-bottom:0}}
.cls-var-tbl{{width:100%;border-collapse:collapse;font-size:12px}}
.cls-var-tbl th{{background:#475569;color:white;padding:6px 10px;text-align:left;font-size:11px}}
.cls-var-tbl td{{padding:6px 10px;border-bottom:1px solid #f1f5f9;vertical-align:top}}
.cls-var-tbl tr:nth-child(even) td{{background:#f8fafc}}
.cls-var-tbl tr:hover td{{background:#eff6ff}}
/* 인터페이스설계서 */
.if-list-tbl{{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:24px}}
.if-list-tbl th{{background:#1e3a5f;color:white;padding:8px 12px;text-align:left;font-size:11px}}
.if-list-tbl td{{padding:8px 12px;border-bottom:1px solid #e2e8f0}}
.if-list-tbl tr:hover td{{background:#f0f9ff}}
.if-detail-block{{background:white;border:1px solid #e2e8f0;border-radius:8px;margin-bottom:16px;overflow:hidden}}
.if-detail-title{{background:linear-gradient(90deg,#0f766e,#0d9488);color:white;padding:9px 16px;font-size:13px;font-weight:700}}
/* AI */
.ai-box{{background:#f0fdf4;border:1px solid #86efac;border-radius:10px;padding:18px;margin-bottom:20px}}
.ai-title{{font-size:14px;font-weight:700;color:#15803d;margin-bottom:10px}}
.ai-body{{font-size:12px;color:#166534;line-height:1.8;white-space:pre-wrap}}
/* ERD */
.erd-wrap{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:20px;overflow-x:auto}}
/* 푸터 */
.footer{{text-align:center;color:#94a3b8;font-size:11px;padding:20px}}
</style></head><body>
<div class="hdr">
  <div class="hdr-icon">📐</div>
  <div>
    <h1>설계 문서 자동 생성</h1>
    <p>소스: {src_dir} {'| 기획서: '+plan_file if plan_file else ''} | 생성: {now}</p>
  </div>
</div>
<div class="nav">
  <button class="nav-btn active" onclick="show('overview',this)">개요</button>
  <button class="nav-btn" onclick="show('erd',this)">ERD ({len(tables)})</button>
  <button class="nav-btn" onclick="show('tables',this)">테이블 정의서 ({len(tables)})</button>
  <button class="nav-btn" onclick="show('apis',this)">API 명세서 ({len(apis)})</button>
  <button class="nav-btn" onclick="show('classes',this)">클래스설계서 ({len(classes)})</button>
  <button class="nav-btn" onclick="show('interfaces',this)">인터페이스설계서 ({len(ifaces)})</button>
</div>
<div class="body">

<div id="overview" class="section active">
  <div class="stats">
    <div class="stat"><div class="stat-lbl">테이블</div><div class="stat-num">{len(tables)}</div><div class="stat-lbl">개</div></div>
    <div class="stat"><div class="stat-lbl">API</div><div class="stat-num">{len(apis)}</div><div class="stat-lbl">개</div></div>
    <div class="stat"><div class="stat-lbl">클래스</div><div class="stat-num">{len(classes)}</div><div class="stat-lbl">개</div></div>
    <div class="stat" style="border-top-color:#059669"><div class="stat-lbl">GET</div><div class="stat-num" style="color:#059669">{sum(1 for a in apis if a['method']=='GET')}</div></div>
    <div class="stat" style="border-top-color:#2563eb"><div class="stat-lbl">POST</div><div class="stat-num" style="color:#2563eb">{sum(1 for a in apis if a['method']=='POST')}</div></div>
    <div class="stat" style="border-top-color:#d97706"><div class="stat-lbl">PUT/DEL</div><div class="stat-num" style="color:#d97706">{sum(1 for a in apis if a['method'] in ('PUT','DELETE'))}</div></div>
  </div>
  {ai_html}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div style="background:white;border:1px solid #e2e8f0;border-radius:10px;padding:16px">
      <div style="font-weight:700;color:#1e3a5f;margin-bottom:10px;font-size:13px">📋 테이블 목록</div>
      {''.join(f'<div style="padding:5px 0;border-bottom:1px solid #f1f5f9;font-size:12px;display:flex;gap:8px"><span style="font-family:monospace;color:#1e3a5f;font-weight:600">{t}</span><span style="color:#64748b">{tables[t].get("korean","")}</span></div>' for t in list(tables.keys())[:25])}
    </div>
    <div style="background:white;border:1px solid #e2e8f0;border-radius:10px;padding:16px">
      <div style="font-weight:700;color:#1e3a5f;margin-bottom:10px;font-size:13px">🔗 API 목록</div>
      {''.join(f'<div style="padding:4px 0;border-bottom:1px solid #f1f5f9;font-size:11px;display:flex;align-items:center;gap:6px"><span style="background:{METHOD_COLOR.get(a["method"],"#64748b")};color:white;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:700;min-width:36px;text-align:center">{a["method"]}</span><span style="font-family:monospace">{a["path"]}</span></div>' for a in apis[:25])}
    </div>
  </div>
</div>

<div id="erd" class="section">
  <div class="sec-title">🗂 ERD (Entity Relationship Diagram)</div>
  <div class="sec-sub">소스코드에서 자동 추출된 테이블 구조. PK 컬럼은 진하게 표시.</div>
  <div class="erd-wrap">{erd_svg}</div>
</div>

<div id="tables" class="section">
  <div class="sec-title">📋 테이블 정의서</div>
  <div class="sec-sub">작성일: {date_str} | 총 {len(tables)}개 테이블</div>
  {tbl_sections or '<div style="color:#94a3b8;padding:40px;text-align:center;background:white;border-radius:10px">MyBatis XML, SQL CREATE TABLE, @Entity 클래스를 찾지 못했습니다</div>'}
</div>

<div id="apis" class="section">
  <div class="sec-title">🔗 API 명세서</div>
  <div class="sec-sub">작성일: {date_str} | 총 {len(apis)}개 엔드포인트</div>
  {api_sections or '<div style="color:#94a3b8;padding:40px;text-align:center;background:white;border-radius:10px">@Controller / @RestController 클래스를 찾지 못했습니다</div>'}
</div>

<div id="classes" class="section">
  <div class="sec-title">🧩 클래스설계서</div>
  <div class="sec-sub">작성일: {date_str} | 총 {len(classes)}개 클래스 / {len(cls_groups)}개 도메인 그룹</div>
  {cls_design_sections or '<div style="color:#94a3b8;padding:40px;text-align:center;background:white;border-radius:10px">Java 클래스 파일을 찾지 못했습니다</div>'}
</div>

<div id="interfaces" class="section">
  <div class="sec-title">🔌 인터페이스설계서</div>
  <div class="sec-sub">작성일: {date_str} | 총 {len(ifaces)}개 인터페이스</div>
  {ifaces_section_html}
</div>

</div>
<div class="footer">설계 문서 자동 생성 도구 v2 | {now}</div>
<script>
function show(id,btn){{
  document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
</script>
</body></html>'''

    Path(output_path).write_text(html, encoding='utf-8')

# ══════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════
METHOD_COLOR = {'GET':'#059669','POST':'#2563eb','PUT':'#d97706',
                'DELETE':'#dc2626','PATCH':'#7c3aed','ALL':'#64748b'}


# ── Gemini API 호출 ──────────────────────────────────────
def call_gemini_api(api_key, tables, apis, classes, plan_text=''):
    try:
        import json, urllib.request
        tbl_names = list(tables.keys())[:15]
        api_sample = [a['method']+' '+a['path'] for a in apis[:8]]
        cls_names = [c['name'] for c in classes[:15]]
        plan_part = ('기획서 내용:\n' + plan_text[:3000]) if plan_text else ''
        prompt = (
            '아래는 Java 프로젝트 소스코드 분석 결과와 기획서입니다.\n'
            + plan_part + '\n\n'
            + '현재 소스 분석 결과:\n'
            + '- 기존 테이블: ' + str(tbl_names) + '\n'
            + '- API 수: ' + str(len(apis)) + '개\n'
            + '- API 샘플: ' + str(api_sample) + '\n'
            + '- 주요 클래스: ' + str(cls_names) + '\n\n'
            + '다음 항목을 한국어로 작성해주세요:\n'
            + '1. 시스템 전체 구조 요약 (3줄)\n'
            + '2. 기획서 기반 신규 추가 필요 테이블 목록 및 설명\n'
            + '3. 기존 테이블 중 변경 필요한 항목 (컬럼 추가/수정)\n'
            + '4. 신규 개발 필요 API 목록\n'
            + '5. 설계 보완 필요 항목'
        )
        payload = json.dumps({
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'maxOutputTokens': 2048}
        }).encode('utf-8')
        url = ('https://generativelanguage.googleapis.com/v1beta'
               '/models/gemini-1.5-flash:generateContent?key=' + api_key)
        req = urllib.request.Request(url, data=payload,
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            return data['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        return 'Gemini API 오류: ' + str(e)

def call_ai_api(api_key, tables, apis, classes, plan_text=''):
    if not api_key:
        return ''
    if api_key.startswith('AIza'):
        return call_gemini_api(api_key, tables, apis, classes, plan_text)
    return call_claude_api(api_key, tables, apis, classes, plan_text)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('설계 문서 자동 생성 도구 v3')
        self.geometry('700x700')
        self.resizable(True, True)
        self.configure(bg='#f1f5f9')
        self.running = False
        self._build()

    def _build(self):
        hdr = tk.Frame(self, bg='#1e3a5f', height=62)
        hdr.pack(fill='x')
        tk.Label(hdr, text='📐  설계 문서 자동 생성 도구 v3', bg='#1e3a5f', fg='white',
                 font=('맑은 고딕',13,'bold')).pack(side='left', padx=20, pady=16)
        tk.Label(hdr, text='ERD · 테이블정의서 · API명세서 · 클래스설계서 · 인터페이스설계서',
                 bg='#1e3a5f', fg='#93c5fd', font=('맑은 고딕',9)).pack(side='left', pady=16)

        body = tk.Frame(self, bg='#f1f5f9', padx=24, pady=18)
        body.pack(fill='both', expand=True)

        self._sv = tk.StringVar()
        self._pv = tk.StringVar()
        self._ov = tk.StringVar(value=str(Path.home()/'Desktop'))
        self._row(body, '① 소스 폴더', self._sv,
                  lambda: self._sv.set(filedialog.askdirectory() or self._sv.get()), '#1d4ed8')
        self._row(body, '② 기획서 파일 (선택)', self._pv,
                  lambda: self._pv.set(filedialog.askopenfilename(
                      filetypes=[('HTML/텍스트','*.html *.htm *.txt *.md'),('전체','*.*')]) or self._pv.get()), '#6366f1')
        self._row(body, '③ 저장 위치', self._ov,
                  lambda: self._ov.set(filedialog.askdirectory() or self._ov.get()), '#64748b')

        self._label(body, '④ API 키 (선택)  Gemini: AIza...  /  Claude: sk-ant...')
        self._ak = tk.StringVar()
        tk.Entry(body, textvariable=self._ak, font=('맑은 고딕',10),
                 relief='solid', bd=1, bg='white', show='*').pack(fill='x', ipady=6, pady=(4,10))

        self._label(body, '⑤ 생성 산출물')
        opt = tk.Frame(body, bg='#f1f5f9'); opt.pack(fill='x', pady=(4,14))
        self._ce = tk.BooleanVar(value=True); self._ct = tk.BooleanVar(value=True)
        self._ca = tk.BooleanVar(value=True); self._cc = tk.BooleanVar(value=True)
        for t, v in [('ERD',self._ce),('테이블 정의서',self._ct),('API 명세서',self._ca),('클래스설계서',self._cc)]:
            tk.Checkbutton(opt, text=t, variable=v, bg='#f1f5f9',
                          font=('맑은 고딕',10), cursor='hand2').pack(side='left', padx=(0,14))

        self.run_btn = tk.Button(body, text='📐  설계 문서 생성', command=self._run,
                                 bg='#1e3a5f', fg='white', font=('맑은 고딕',13,'bold'),
                                 relief='flat', padx=20, pady=10, cursor='hand2')
        self.run_btn.pack(fill='x', pady=(0,12))
        self.pb = ttk.Progressbar(body, mode='indeterminate')
        self.pb.pack(fill='x', pady=(0,10))
        self._label(body, '실행 로그')
        self.log = scrolledtext.ScrolledText(body, height=10, font=('Consolas',9),
                                             bg='#0f172a', fg='#94a3b8', relief='flat', bd=0, state='disabled')
        self.log.pack(fill='both', expand=True, pady=(4,0))
        for t,c in [('ok','#34d399'),('err','#f87171'),('warn','#fbbf24'),('info','#60a5fa')]:
            self.log.tag_config(t, foreground=c)

    def _label(self, p, t):
        tk.Label(p, text=t, bg='#f1f5f9', fg='#475569',
                 font=('맑은 고딕',10,'bold')).pack(anchor='w', pady=(0,2))

    def _row(self, p, label, var, cmd, color):
        self._label(p, label)
        r = tk.Frame(p, bg='#f1f5f9'); r.pack(fill='x', pady=(4,10))
        tk.Entry(r, textvariable=var, font=('맑은 고딕',10), relief='solid', bd=1, bg='white'
                ).pack(side='left', fill='x', expand=True, ipady=6, padx=(0,8))
        tk.Button(r, text='찾아보기', command=cmd, bg=color, fg='white',
                  font=('맑은 고딕',10,'bold'), relief='flat', padx=12, cursor='hand2').pack(side='left')

    def _log(self, msg, tag='info'):
        self.log.config(state='normal')
        ts = datetime.now().strftime('%H:%M:%S')
        self.log.insert('end', f'[{ts}] {msg}\n', tag)
        self.log.see('end')
        self.log.config(state='disabled')

    def _run(self):
        if self.running: return
        src = self._sv.get().strip()
        if not src or not Path(src).exists():
            messagebox.showwarning('경고','소스 폴더를 선택해주세요'); return
        self.running = True
        self.run_btn.config(state='disabled', text='생성 중...')
        self.pb.start(10)
        threading.Thread(target=self._generate, daemon=True).start()

    def _generate(self):
        try:
            src = Path(self._sv.get())
            plan = self._pv.get().strip()
            out_dir = Path(self._ov.get())
            api_key = self._ak.get().strip()
            out_file = out_dir / f'design_doc_{datetime.now().strftime("%Y%m%d_%H%M%S")}.html'

            self._log(f'소스: {src}', 'info')
            self._log('[1/5] 파일 수집 중...', 'info')
            files = collect_files(src)
            self._log(f'      → {len(files)}개 파일', 'ok')

            tables, apis, classes = {}, [], []
            if self._ct.get() or self._ce.get():
                self._log('[2/5] 테이블 구조 추출 중...', 'info')
                tables = extract_tables(files, src)
                self._log(f'      → {len(tables)}개 테이블', 'ok' if tables else 'warn')
            if self._ca.get():
                self._log('[3/5] API 추출 중...', 'info')
                apis = extract_apis(files, src)
                self._log(f'      → {len(apis)}개 API', 'ok' if apis else 'warn')
            if self._cc.get():
                self._log('[4/5] 클래스 분석 중...', 'info')
                classes = extract_classes(files, src)
                self._log(f'      → {len(classes)}개 클래스', 'ok')

            ai_result = ''
            if api_key:
                self._log('[5/5] AI 분석 보완 중...', 'info')
                plan_text = ''
                if plan:
                    try:
                        p = HTMLParser.__new__(HTMLParser)
                        HTMLParser.__init__(p)
                        p.text = []; p._skip = False
                        def hs(tag,a): p._skip = tag in ('style','script')
                        def he(tag): p._skip = tag in ('style','script')
                        def hd(d):
                            if not p._skip:
                                dd=d.strip()
                                if dd: p.text.append(dd)
                        p.handle_starttag=hs; p.handle_endtag=he; p.handle_data=hd
                        p.feed(Path(plan).read_text(encoding='utf-8',errors='ignore'))
                        plan_text = '\n'.join(p.text)
                    except: pass
                ai_result = call_ai_api(api_key, tables, apis, classes, plan_text)
                self._log('      → AI 분석 완료', 'ok')
            else:
                self._log('[5/5] API 키 없음 — 규칙 기반으로만 생성 (Gemini/Claude 키 입력시 AI 분석 추가)', 'warn')

            self._log('리포트 생성 중...', 'info')
            generate_report(tables, apis, classes, ai_result, str(src), plan, str(out_file), files=files)
            self._log(f'✅ 완료! → {out_file}', 'ok')

            import webbrowser
            webbrowser.open(out_file.as_uri())
            messagebox.showinfo('완료', f'설계 문서 생성 완료!\n\n{out_file}')

        except Exception as e:
            import traceback
            self._log(f'오류: {e}', 'err')
            self._log(traceback.format_exc(), 'err')
            messagebox.showerror('오류', str(e))
        finally:
            self.running = False
            self.run_btn.config(state='normal', text='📐  설계 문서 생성')
            self.pb.stop()

if __name__ == '__main__':
    App().mainloop()
