#!/usr/bin/env python3
"""Rebuild doors/data.js from the architect's door schedule PDF.

    pip install pdfplumber
    python3 doors/tools/build_data.py path/to/door_schedule.pdf

Reads every schedule table page, keeps the text, the red "confirm on site"
colouring and the row highlight colours, and writes ../data.js.
"""
import collections
import json
import os
import re
import sys

import pdfplumber

COLS = ['survey', 'surveySize', 'cond', 'ref', 'loc', 'type', 'fire', 'acoustic',
        'soW', 'soH', 'leafW', 'leafH', 'leafD', 'closer', 'hinges', 'access',
        'signage', 'vp', 'arch', 'desc', 'finish', 'comments', 'rev']
DIMS = ['soW', 'soH', 'leafW', 'leafH', 'leafD']


def colour(c):
    if c is None:
        return None
    if isinstance(c, (int, float)):
        c = (c,)
    return tuple(round(float(x), 2) for x in c)


def is_red(c):
    c = colour(c)
    return c is not None and len(c) == 3 and c[0] > 0.55 and c[1] < 0.3 and c[2] < 0.3


def fill_kind(c):
    """Map a cell background colour to its meaning in the schedule key."""
    if not c or len(c) != 3:
        if c and len(c) == 1 and 0.7 < c[0] < 0.9:
            return 'grey'
        return None
    r, g, b = c
    if r > 0.9 and 0.5 < g < 0.7 and b < 0.7:
        return 'red'        # to be demolished / omitted
    if g > 0.9 and r < 0.85:
        return 'green'      # already picked up in Section 01 pack
    return None


def cell_lines(chars, box):
    """Text lines inside a cell: [text, red?, bold?, fills-the-cell-width?]."""
    x0, top, x1, bot = box
    inside = [c for c in chars
              if x0 - 0.5 <= (c['x0'] + c['x1']) / 2 <= x1 + 0.5
              and top - 0.5 <= (c['top'] + c['bottom']) / 2 <= bot + 0.5]
    rows = []
    for c in sorted(inside, key=lambda c: (round(c['top']), c['x0'])):
        if rows and round(c['top']) - rows[-1][0] <= 2:
            rows[-1][1].append(c)
        else:
            rows.append([round(c['top']), [c]])
    out = []
    for _, lc in rows:
        lc.sort(key=lambda c: c['x0'])
        s, prev = '', None
        for c in lc:
            if prev is not None and c['x0'] - prev['x1'] > 0.8 and not s.endswith(' ') and c['text'] != ' ':
                s += ' '
            s += c['text']
            prev = c
        s = s.strip()
        if not s:
            continue
        ink = [c for c in lc if c['text'].strip()]
        red = sum(is_red(c['non_stroking_color']) for c in ink) > len(ink) / 2
        bold = sum('Bold' in c['fontname'] for c in ink) > len(ink) / 2
        width = (max(c['x1'] for c in ink) - min(c['x0'] for c in ink)) / max(1.0, x1 - x0)
        out.append([s, red, bold, width > 0.78])
    return out


def join(lines):
    s = ''
    for text, *_ in lines:
        if not s:
            s = text
        elif s.endswith('-') and not s.endswith(' -'):
            s += text
        else:
            s += ' ' + text
    return re.sub(r'\s+', ' ', s).strip()


def statements(lines):
    """Re-flow wrapped comment lines into separate statements, keeping emphasis."""
    out = []
    for text, red, bold, full in lines:
        prev = out[-1] if out else None
        cont = (prev is not None and prev['red'] == red and prev['bold'] == bold
                and (text[0].islower()
                     or (prev['_full'] and not prev['t'].rstrip().endswith(('.', ';')))))
        if cont:
            glue = '' if prev['t'].endswith('-') and not prev['t'].endswith(' -') else ' '
            prev['t'] += glue + text
            prev['_full'] = full
        else:
            out.append({'t': text, 'red': red, 'bold': bold, '_full': full})
    res = []
    for o in out:
        d = {'t': o['t']}
        if o['red']:
            d['red'] = 1
        elif o['bold']:
            d['bold'] = 1
        res.append(d)
    return res


def main(pdf_path):
    pdf = pdfplumber.open(pdf_path)
    doors, notes, floor = [], [], None
    meta = {}
    for pn, page in enumerate(pdf.pages, 1):
        tables = page.find_tables()
        if not tables:
            continue
        table = max(tables, key=lambda t: len(t.rows))
        rects = [r for r in page.rects if r.get('fill') and r.get('non_stroking_color') is not None]
        chars = page.chars

        if not meta:
            head = page.crop((0, 0, page.width, table.bbox[1])).extract_text() or ''
            for key, pat in (('docRef', r'Doc Ref\s*:\s*(\S+)'), ('rev', r'Rev\s*:\s*(\S+?)/'),
                             ('status', r'Status\s*:\s*(\w+)'), ('date', r'Date\s*:\s*(\d\d-\w+-\d{4})'),
                             ('project', r'(\d{4} - [^\n]+Hotel)')):
                m = re.search(pat, head)
                if m:
                    meta[key] = m.group(1).strip()

        def background(box):
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            best, area = None, 1e18
            for r in rects:
                if r['x0'] <= cx <= r['x1'] and r['top'] <= cy <= r['bottom']:
                    a = (r['x1'] - r['x0']) * (r['bottom'] - r['top'])
                    if a < area:
                        area, best = a, colour(r['non_stroking_color'])
            return best

        for row in table.rows:
            boxes = [b for b in row.cells if b is not None]
            cells = [cell_lines(chars, b) for b in boxes]
            first = join(cells[0]) if cells else ''
            if first.startswith('Notes'):
                if not notes:
                    for text, red, bold, _ in cells[0][1:]:
                        if text.startswith('-') or '=' in text or not notes:
                            notes.append({'t': text.lstrip('- ').strip(), 'key': 1 if (red or bold) else 0})
                        else:
                            notes[-1]['t'] += ' ' + text
                continue
            if first.startswith(('Doorsets', 'FIRE DOOR')) or row.cells[0] is None:
                continue
            fills = [fill_kind(background(b)) for b in boxes]
            if len(boxes) == 1:
                floor = first
                continue
            if len(boxes) < 20:
                # merged "X. demolished" row: survey no + size, then the note
                m = re.match(r'(\S+)\s+(.*)', first)
                doors.append({'floor': floor, 'page': pn, 'ref': '', 'survey': m.group(1) if m else first,
                              'surveySize': m.group(2) if m else '', 'loc': 'Demolished',
                              'status': 'demolished', 'cond': 'X'})
                continue
            while len(cells) < len(COLS):      # a row with the comments cell merged away
                cells.insert(COLS.index('comments'), [])
            d = {'floor': floor, 'page': pn}
            for name, lines in zip(COLS, cells):
                if name == 'comments':
                    st = statements(lines)
                    if st:
                        d[name] = st
                else:
                    v = join(lines)
                    if v:
                        d[name] = v
            d['tbc'] = [n for n, lines in zip(COLS, cells) if n in DIMS and any(l[1] for l in lines)]
            if not d['tbc']:
                del d['tbc']
            for k in ('access', 'arch', 'vp', 'signage'):
                i = COLS.index(k)
                if cells[i] and all(l[1] for l in cells[i]):
                    d.setdefault('redFields', []).append(k)

            cond = d.get('cond', '')
            ref = d.get('ref', '')
            blob = ' '.join(str(d.get(k, '')) for k in ('cond', 'ref', 'type', 'fire')).upper()
            if 'DEMOLISH' in blob or ref.upper().startswith('X'):
                status = 'demolished'
            elif 'OMITTED' in blob or 'red' in fills[2:6]:
                status = 'omitted'
            elif 'green' in fills:
                status = 'sec01'    # struck through: dealt with in the Section 01 pack
            elif 'ABEY' in blob:
                status = 'abeyance'
            else:
                status = 'active'
            d['status'] = status
            if 'ALREADY ON' in cond.upper():
                d['onsite'] = 1
            d['ref'] = re.sub(r'\s*(IN ABEY\w+|OMITTED)', '', ref).strip()
            c0 = cond.strip().upper()[:1]
            d['cond'] = c0 if c0 in ('N', 'E') else ''
            doors.append(d)

    for i, d in enumerate(doors):
        d['id'] = i
    meta['count'] = len(doors)
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, '..', 'data.js'), 'w', encoding='utf-8') as f:
        f.write('// Generated by tools/build_data.py from the door schedule PDF. Do not edit by hand.\n')
        f.write('window.SCHEDULE = ' + json.dumps({'meta': meta, 'notes': notes, 'doors': doors},
                                                  ensure_ascii=False, separators=(',', ':')) + ';\n')
    print(meta)
    print(collections.Counter(d['status'] for d in doors))
    print(collections.Counter(d['floor'] for d in doors))


if __name__ == '__main__':
    main(sys.argv[1])
