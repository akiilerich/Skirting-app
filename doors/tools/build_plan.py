#!/usr/bin/env python3
"""Add a GA plan drawing to the Door Schedule tool.

    pip install pdfplumber pillow        (and poppler-utils for pdftocairo / pdftoppm)
    python3 doors/tools/build_plan.py path/to/GA_plan.pdf "First Floor"

The floor name must match a floor heading in the schedule. For that floor this
writes ../plans/<floor>.svg (the drawing, cropped to the building) and updates
../plan-data.js with, per door tag found on the drawing:
  - where the door is on the plan
  - its swing (hinge side, which way it opens), read from the dashed swing arc
    and kept only when the arc's radius agrees with the schedule's leaf width.
Run build_data.py first: the schedule is used to check the swings.
"""
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile

import pdfplumber
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
MARGIN = 130            # pt of drawing kept around the outermost door tags
WIDTH_TOLERANCE = 40    # mm between measured swing radius and scheduled leaf width


def load_js(name, marker):
    path = os.path.join(HERE, '..', name)
    if not os.path.exists(path):
        return None
    s = open(path, encoding='utf-8').read()
    return json.loads(s[s.index(marker) + len(marker):].rstrip().rstrip(';'))


def is_swing_grey(o):
    c = o.get('stroking_color')
    if isinstance(c, (int, float)):         # some drawings store greys as one value, not RGB
        c = (c,)
    return (isinstance(c, (tuple, list)) and len(c) in (1, 3) and abs(c[0] - 0.597) < 0.012
            and (len(c) == 1 or abs(c[2] - 0.596) < 0.012)
            and abs((o.get('linewidth') or 0) - 0.12) < 0.02)


def circle3(a, b, c):
    ax, ay = a; bx, by = b; cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-6:
        return None
    ux = ((ax*ax + ay*ay) * (by - cy) + (bx*bx + by*by) * (cy - ay) + (cx*cx + cy*cy) * (ay - by)) / d
    uy = ((ax*ax + ay*ay) * (cx - bx) + (bx*bx + by*by) * (ax - cx) + (cx*cx + cy*cy) * (bx - ax)) / d
    return ux, uy, math.hypot(ax - ux, ay - uy)


def find_swings(page):
    """Door swings are drawn as dashed grey quarter-circles plus a dashed open leaf."""
    random.seed(1)
    polys = [o for o in page.curves if is_swing_grey(o) and not o.get('fill')]
    pts, straight = [], []
    for o in polys:
        pts.extend(o['pts']); straight.extend(o['pts'])
    for o in page.lines:
        if is_swing_grey(o):
            straight.extend(o['pts'])
            (x0, y0), (x1, y1) = o['pts'][0], o['pts'][1]
            if math.hypot(x0 - x1, y0 - y1) < 3.5:
                pts.extend(o['pts'])
    pts = list(dict.fromkeys((round(x, 2), round(y, 2)) for x, y in pts))
    remaining, arcs = set(pts), []
    for seed in pts:
        if seed not in remaining:
            continue
        local = [q for q in remaining if abs(q[0] - seed[0]) < 70 and abs(q[1] - seed[1]) < 70]
        if len(local) < 14:
            continue
        best = None
        for _ in range(300):
            b, c = random.sample(local, 2)
            if b == seed or c == seed:
                continue
            f = circle3(seed, b, c)
            if not f or not 9 < f[2] < 45:
                continue
            inl = [q for q in local if abs(math.hypot(q[0] - f[0], q[1] - f[1]) - f[2]) < 0.18]
            if best is None or len(inl) > len(best[1]):
                best = (f, inl)
        if not best or len(best[1]) < 14:
            continue
        (cx, cy, r), inl = best
        ang = sorted(math.atan2(y - cy, x - cx) for x, y in inl)
        gaps = [(ang[(i + 1) % len(ang)] - ang[i]) % (2 * math.pi) for i in range(len(ang))]
        gi = max(range(len(gaps)), key=lambda i: gaps[i])
        span = 2 * math.pi - gaps[gi]
        if not 50 < math.degrees(span) < 115 or math.degrees(sorted(gaps)[-2]) > 12:
            continue
        for q in inl:
            remaining.discard(q)
        a0 = ang[(gi + 1) % len(ang)]
        ends = [(cx + r * math.cos(t), cy + r * math.sin(t)) for t in (a0, a0 + span)]

        def leaf_marks(e):      # dashes lying along hinge -> arc end: that end is the drawn (open) leaf
            n = 0
            for x, y in straight:
                t = ((x - cx) * (e[0] - cx) + (y - cy) * (e[1] - cy)) / (r * r)
                if 0.1 < t < 0.98 and abs((e[0] - cx) * (cy - y) - (cx - x) * (e[1] - cy)) / r < 1.6:
                    n += 1
            return n
        oi = 0 if leaf_marks(ends[0]) >= leaf_marks(ends[1]) else 1
        arcs.append({'c': (cx, cy), 'r': r, 'open': ends[oi], 'closed': ends[1 - oi]})
    return arcs


def ocr_tags(pdf_path, page, refs, tmp):
    """Some drawings (e.g. Lower Ground) have their text drawn as outlines, so there is no text layer.
    Read the tags off a raster instead. Only a tag that exactly matches a scheduled ref, with good
    confidence, and that appears once on the sheet is accepted: a misread then costs a door its plan
    view rather than putting the marker on the wrong door."""
    try:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        sys.exit('This drawing has no text layer. Run: pip install rapidocr-onnxruntime')
    dpi = 300
    subprocess.run(['pdftoppm', '-r', str(dpi), '-png', '-singlefile', pdf_path, os.path.join(tmp, 'ocr')], check=True)
    Image.MAX_IMAGE_PIXELS = None
    big = Image.open(os.path.join(tmp, 'ocr.png')).convert('RGB')
    engine, k = RapidOCR(), dpi / 72.0
    step, overlap = 2200, 200
    hits = []
    for y in range(0, big.size[1], step - overlap):
        for x in range(0, big.size[0], step - overlap):
            res, _ = engine(np.array(big.crop((x, y, min(big.size[0], x + step), min(big.size[1], y + step)))))
            for box, txt, conf in (res or []):
                t = re.sub(r'\s+', '', txt).upper()
                if t in refs and float(conf) >= 0.7:
                    cx = x + sum(pt[0] for pt in box) / 4.0
                    cy = y + sum(pt[1] for pt in box) / 4.0
                    hits.append((t, cx / k, cy / k))
    found = {}
    for t, cx, cy in hits:
        found.setdefault(t, []).append((cx, cy))
    tags, dropped = {}, []
    for t, pts_ in found.items():
        merged = []
        for pt in pts_:                               # the tile overlap can read one tag twice
            if not any(math.hypot(pt[0] - m[0], pt[1] - m[1]) < 12 for m in merged):
                merged.append(pt)
        if len(merged) == 1:
            tags[t] = merged[0]
        else:
            dropped.append(t)
    if dropped:
        print('OCR tags read in more than one place, ignored:', ', '.join(sorted(dropped)))
    return tags


def main(pdf_path, floor):
    schedule = load_js('data.js', 'window.SCHEDULE = ')
    sched = {d['ref']: d for d in schedule['doors'] if d['floor'] == floor and d.get('ref')}
    if not sched:
        sys.exit('No floor called %r in the schedule' % floor)
    page = pdfplumber.open(pdf_path).pages[0]
    scale = None
    text = page.extract_text() or ''
    m = re.search(r'1:(\d+)\s*@', text)
    scale = int(m.group(1)) if m else 100
    mm_per_pt = 25.4 / 72 * scale
    tags = {w['text']: ((w['x0'] + w['x1']) / 2, (w['top'] + w['bottom']) / 2)
            for w in page.extract_words() if w['text'] in sched}
    tmp = tempfile.mkdtemp()
    ocr_used = False
    if not tags:
        tags = ocr_tags(pdf_path, page, set(sched), tmp)
        ocr_used = True
    if not tags:
        sys.exit('No door tags from this floor found on the drawing')

    # low-res raster, only used to tell corridor fill from room fill either side of a door
    subprocess.run(['pdftoppm', '-r', '150', '-png', '-singlefile', pdf_path, os.path.join(tmp, 'p')], check=True)
    im = Image.open(os.path.join(tmp, 'p.png')).convert('RGB')
    k = im.size[0] / float(page.width)

    def side(x, y):
        r, g, b = im.getpixel((min(im.size[0] - 1, max(0, int(x * k))), min(im.size[1] - 1, max(0, int(y * k)))))
        if r > 240 and 180 < g < 220 and b < 140:
            return 'corridor'
        return 'white' if min(r, g, b) > 245 else 'room'

    arcs = find_swings(page)
    for a in arcs:
        (cx, cy), (ox, oy), (kx, ky), r = a['c'], a['open'], a['closed'], a['r']
        a['mid'] = ((cx + kx) / 2, (cy + ky) / 2)
        bx, by = cx + (ox - cx) * .5 + (kx - cx) * .5, cy + (oy - cy) * .5 + (ky - cy) * .5   # inside the swing
        ux, uy = (kx - cx) / r, (ky - cy) / r
        t = (bx - cx) * ux + (by - cy) * uy
        px, py = cx + t * ux, cy + t * uy
        mx, my = 2 * px - bx, 2 * py - by                                                    # the push side
        a['into'], a['from'] = side(bx, by), side(mx, my)
        a['hinge_push'] = 'right' if (px - mx) * (cy - py) - (py - my) * (cx - px) > 0 else 'left'
        a['w'] = round(r * mm_per_pt)

    pairs = sorted((math.hypot(p[0] - a['mid'][0], p[1] - a['mid'][1]), ref, i)
                   for ref, p in tags.items() for i, a in enumerate(arcs))
    used, got = set(), {}
    for dist, ref, i in pairs:
        double = 'double' in sched[ref].get('type', '').lower()
        if dist > 30 or i in used or len(got.get(ref, [])) >= (2 if double else 1):
            continue
        used.add(i); got.setdefault(ref, []).append(i)

    doors, verified = {}, 0
    for ref, (tx, ty) in sorted(tags.items()):
        entry = {'f': floor, 'x': round(tx, 1), 'y': round(ty, 1)}
        mine = [arcs[i] for i in got.get(ref, [])]
        m = re.match(r'\d+', sched[ref].get('leafW', ''))
        double = 'double' in sched[ref].get('type', '').lower()
        if mine:
            entry['x'] = round(sum(a['mid'][0] for a in mine) / len(mine), 1)
            entry['y'] = round(sum(a['mid'][1] for a in mine) / len(mine), 1)
        live = not sched[ref].get('off') and sched[ref]['status'] in ('active', 'abeyance')
        good = (mine and m and len(mine) == (2 if double else 1) and live
                and all(abs(a['w'] - int(m.group())) <= WIDTH_TOLERANCE for a in mine))
        # The schedule gives no leaf width for some doors ("existing", "as required..."), so the arc cannot
        # be cross-checked. Accept it only if it is plainly this door's: close to the tag, one arc, and no
        # other arc nearby that it could be confused with. These are labelled as unchecked.
        unchecked = False
        if not good and live and not m and not double and len(mine) == 1:
            a = mine[0]
            d_own = math.hypot(tags[ref][0] - a['mid'][0], tags[ref][1] - a['mid'][1])
            others = [math.hypot(tags[ref][0] - b['mid'][0], tags[ref][1] - b['mid'][1])
                      for b in arcs if b is not a]
            if d_own <= 20 and (not others or min(others) - d_own >= 15) and 500 <= a['w'] <= 1000:
                good = unchecked = True
        if good:
            a = mine[0]
            if a['into'] == 'room' and a['from'] == 'corridor':
                way = 'in'
            elif a['into'] == 'corridor' and a['from'] != 'corridor':
                way = 'out'
            else:
                way = ''
            sw = {'way': way}
            if not double:
                # describe it from the corridor side: pushing if it opens in, pulling if it opens out
                if way == 'out':
                    sw['hinge'], sw['as'] = ('left' if a['hinge_push'] == 'right' else 'right'), 'pull'
                else:
                    sw['hinge'], sw['as'] = a['hinge_push'], 'push'
            if unchecked:
                sw['chk'] = 0
            entry['sw'] = sw
            verified += 1
        doors[ref] = entry

    xs = [d['x'] for d in doors.values()]; ys = [d['y'] for d in doors.values()]
    x0, y0 = max(0, min(xs) - MARGIN), max(0, min(ys) - MARGIN)
    x1, y1 = min(float(page.width), max(xs) + MARGIN), min(float(page.height), max(ys) + MARGIN)
    slug = re.sub(r'[^a-z0-9]+', '-', floor.lower()).strip('-')
    svg_path = os.path.join(HERE, '..', 'plans', slug + '.svg')
    os.makedirs(os.path.dirname(svg_path), exist_ok=True)
    raw = os.path.join(tmp, 'p.svg')
    subprocess.run(['pdftocairo', '-svg', pdf_path, raw], check=True)
    svg = open(raw, encoding='utf-8').read()
    # door swings are hairline light-grey dashes on the drawing: too faint on a phone, so darken and thicken them
    def darken(m):
        # effective line width on the sheet is ~0.31 pt either way: "1" is scaled by 0.12, "0.06" is not
        return m.group(1) + ('2.6' if m.group(2) == '1' else '0.31') + m.group(3) + 'rgb(25%, 25%, 25%)'
    svg = re.sub(r'(<path fill="none" stroke-width=")(1|0\.06)("[^>]*?stroke=")rgb\(5[5-9]\.\d*%, 5[5-9]\.\d*%, 5[5-9]\.\d*%\)',
                 darken, svg)
    head_end = svg.index('>', svg.index('<svg'))
    head = svg[svg.index('<svg'):head_end]
    head = re.sub(r'\swidth="[^"]*"', '', head)
    head = re.sub(r'\sheight="[^"]*"', '', head)
    head = re.sub(r'\sviewBox="[^"]*"', '', head)
    head += ' width="%.0f" height="%.0f" viewBox="%.1f %.1f %.1f %.1f"' % (x1 - x0, y1 - y0, x0, y0, x1 - x0, y1 - y0)
    open(svg_path, 'w', encoding='utf-8').write(svg[:svg.index('<svg')] + head + svg[head_end:])

    plans = load_js('plan-data.js', 'window.PLANS = ') or {'floors': {}, 'doors': {}}
    plans['doors'] = {r: d for r, d in plans['doors'].items() if d['f'] != floor}
    plans['doors'].update(doors)
    # sheet number and revision: from the drawing text, or (outlined-text drawings) from the file name,
    # which follows the architect's 11072-TDH-ARC-A-<level>-DR-GA-<sheet>-<rev> convention
    stem = os.path.basename(pdf_path)
    dm = re.search(r'\b\d{4}-G\d-\d+\b', text) or re.search(r'-DR-GA-(0G\d+)', stem)
    rm = re.search(r'-DR-GA-\w+-([PT]\d+)', text) or re.search(r'-DR-GA-\w+?-([PT]\d+)(?:_|\.)', stem)
    plans['floors'][floor] = {'src': 'plans/%s.svg' % slug, 'x': round(x0, 1), 'y': round(y0, 1),
                              'w': round(x1 - x0, 1), 'h': round(y1 - y0, 1), 'mmPerPt': round(mm_per_pt, 2),
                              'drawing': (dm.group(1) if dm and dm.lastindex else dm.group(0)) if dm else '', 'rev': rm.group(1) if rm else ''}
    with open(os.path.join(HERE, '..', 'plan-data.js'), 'w', encoding='utf-8') as f:
        f.write('// Generated by tools/build_plan.py from the GA plan drawings. Do not edit by hand.\n')
        f.write('window.PLANS = ' + json.dumps(plans, ensure_ascii=False, separators=(',', ':')) + ';\n')

    n_unchecked = sum(1 for d in doors.values() if d.get('sw', {}).get('chk') == 0)
    print('%s: %d doors located%s, %d swings read (%d checked against the scheduled width, %d not checkable)' % (
        floor, len(doors), ' by OCR' if ocr_used else '', verified, verified - n_unchecked, n_unchecked))
    print('located only (no verified swing):', ', '.join(r for r, d in doors.items() if 'sw' not in d))
    print('in schedule but not tagged on this drawing:', ', '.join(sorted(set(sched) - set(tags))))
    print('wrote', os.path.relpath(svg_path), os.path.getsize(svg_path) // 1024, 'KB')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
