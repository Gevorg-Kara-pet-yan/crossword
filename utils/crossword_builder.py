import random


def generate_crossword_grid(qa_list, keyword):
    """
    Վերադարձնում է dict { 'grid': trimmed_grid, 'placed': placed_words, 'rows': R, 'cols': C }
    qa_list: list of {'q': question, 'a': ANSWER_UPPER}
    keyword: string (already uppercased) կամ '' եթե ոչ
    Լեգասի output ֆորմատը պահպանված է (placed items have keys word,x,y,dir,question,number,type)
    """

    if not qa_list and not keyword:
        return {'grid': [], 'placed': [], 'rows': 0, 'cols': 0}

    words = [{'word': qa['a'], 'question': qa.get('q', '')} for qa in qa_list]

    # ---------- 1) Keyword mode with dynamic intersections ----------
    if keyword:
        max_word_len = max((len(w['word']) for w in words), default=0)
        word_count = len(words)

        grid_height = max(len(keyword), word_count * (max_word_len // 2 + 1)) + 10
        grid_width = max(len(keyword), max_word_len + 5) + 10

        grid = [['' for _ in range(grid_width)] for _ in range(grid_height)]
        placed_words = []

        # place keyword vertically centered
        keyword_col = grid_width // 2
        keyword_row = (grid_height - len(keyword)) // 2
        for i, ch in enumerate(keyword):
            grid[keyword_row + i][keyword_col] = ch

        placed_words.append({
            'word': keyword,
            'x': keyword_col,
            'y': keyword_row,
            'dir': 'v',
            'question': 'Բանալի բառ',
            'number': None,
            'type': 'keyword'
        })

        clue_number = 1
        for idx, w in enumerate(words):
            word = w['word']
            question = w['question']
            placed = False

            # փորձենք գտնել word-ի ցանկացած տառը, որը համընկնում է keyword[idx]-ի հետ
            if idx < len(keyword):
                kw_char = keyword[idx]
                possible_indices = [i for i, ch in enumerate(word) if ch == kw_char]
                random.shuffle(possible_indices)
                for wi in possible_indices:
                    start_row = keyword_row + idx
                    start_col = keyword_col - wi  # հորիզոնական տեղավորում
                    # ստուգենք տեղավորումը
                    can_place = True
                    for j, ch in enumerate(word):
                        r, c = start_row, start_col + j
                        if c < 0 or c >= grid_width or r < 0 or r >= grid_height:
                            can_place = False
                            break
                        if grid[r][c] not in ('', ch):
                            can_place = False
                            break
                    if can_place:
                        for j, ch in enumerate(word):
                            r, c = start_row, start_col + j
                            grid[r][c] = ch
                        placed_words.append({
                            'word': word,
                            'x': start_col,
                            'y': start_row,
                            'dir': 'h',
                            'question': question,
                            'number': clue_number,
                            'type': 'normal'
                        })
                        clue_number += 1
                        placed = True
                        break

            # fallback եթե ոչ մի տառ չի հատվել
            if not placed:
                for row in range(grid_height):
                    empty_row = True
                    for col in range(len(word)):
                        if col < grid_width and grid[row][col] != '':
                            empty_row = False
                            break
                    if empty_row and len(word) <= grid_width:
                        for i, ch in enumerate(word):
                            grid[row][i] = ch
                        placed_words.append({
                            'word': word,
                            'x': 0,
                            'y': row,
                            'dir': 'h',
                            'question': question,
                            'number': clue_number,
                            'type': 'normal'
                        })
                        clue_number += 1
                        break

        # trim minimal bounding box
        min_row, max_row = grid_height, -1
        min_col, max_col = grid_width, -1
        for r in range(grid_height):
            for c in range(grid_width):
                if grid[r][c] != '':
                    min_row = min(min_row, r)
                    max_row = max(max_row, r)
                    min_col = min(min_col, c)
                    max_col = max(max_col, c)
        if min_row == grid_height:
            return {'grid': [], 'placed': [], 'rows': 0, 'cols': 0}

        trimmed_grid = [
            [grid[r][c] for c in range(min_col, max_col + 1)]
            for r in range(min_row, max_row + 1)
        ]

        for p in placed_words:
            p['x'] -= min_col
            p['y'] -= min_row

        return {
            'grid': trimmed_grid,
            'placed': placed_words,
            'rows': len(trimmed_grid),
            'cols': len(trimmed_grid[0]) if trimmed_grid else 0
        }

    # ---------- 2) NO-KEYWORD mode (original logic) ----------
    sparse = {}
    placed = []
    words_sorted = sorted(words, key=lambda x: len(x['word']), reverse=True)

    def can_place(word, r, c, dir):
        dr, dc = (0, 1) if dir == 'h' else (1, 0)
        for i, ch in enumerate(word):
            rr, cc = r + dr * i, c + dc * i
            cur = sparse.get((rr, cc))
            if cur is None:
                if dir == 'h':
                    up = sparse.get((rr - 1, cc))
                    down = sparse.get((rr + 1, cc))
                    if (up is not None and up != ch) or (down is not None and down != ch):
                        return False
                else:
                    left = sparse.get((rr, cc - 1))
                    right = sparse.get((rr, cc + 1))
                    if (left is not None and left != ch) or (right is not None and right != ch):
                        return False
            else:
                if cur != ch:
                    return False
        before = (r - dr, c - dc)
        after = (r + dr * len(word), c + dc * len(word))
        if sparse.get(before) is not None or sparse.get(after) is not None:
            return False
        return True

    def place_sparse(word, r, c, dir):
        dr, dc = (0, 1) if dir == 'h' else (1, 0)
        for i, ch in enumerate(word):
            sparse[(r + dr * i, c + dc * i)] = ch

    if words_sorted:
        first = words_sorted[0]
        place_sparse(first['word'], 0, 0, 'h')
        placed.append({'word': first['word'], 'x': 0, 'y': 0, 'dir': 'h', 'question': first['question'], 'number': 1,
                       'type': 'normal'})
        next_number = 2
    else:
        return {'grid': [], 'placed': [], 'rows': 0, 'cols': 0}

    for w in words_sorted[1:]:
        word = w['word']
        question = w['question']
        placed_ok = False
        candidates = []
        for wi, ch in enumerate(word):
            for (rr, cc), gch in list(sparse.items()):
                if gch == ch:
                    candidates.append(('h', rr, cc - wi))
                    candidates.append(('v', rr - wi, cc))
        random.shuffle(candidates)
        for dir, rs, cs in candidates:
            if can_place(word, rs, cs, dir):
                place_sparse(word, rs, cs, dir)
                placed.append({'word': word, 'x': cs, 'y': rs, 'dir': dir, 'question': question, 'number': next_number,
                               'type': 'normal'})
                next_number += 1
                placed_ok = True
                break
        if placed_ok:
            continue
        if sparse:
            rows = [p[0] for p in sparse.keys()]
            cols = [p[1] for p in sparse.keys()]
            rmin, rmax, cmin, cmax = min(rows), max(rows), min(cols), max(cols)
        else:
            rmin = cmin = rmax = cmax = 0
        PAD = max(3, len(word))
        free_positions = []
        for r in range(rmin - PAD, rmax + PAD + 1):
            for c in range(cmin - PAD, cmax + PAD + 1):
                for dir in ('h', 'v'):
                    if can_place(word, r, c, dir):
                        free_positions.append((dir, r, c))
        random.shuffle(free_positions)
        if free_positions:
            dir, rs, cs = free_positions[0]
            place_sparse(word, rs, cs, dir)
            placed.append({'word': word, 'x': cs, 'y': rs, 'dir': dir, 'question': question, 'number': next_number,
                           'type': 'normal'})
            next_number += 1
            continue
        attempts = 0
        placed_try = False
        while attempts < 300 and not placed_try:
            attempts += 1
            r = random.randint(rmin - PAD * 3, rmax + PAD * 3)
            c = random.randint(cmin - PAD * 3, cmax + PAD * 3)
            dir = random.choice(('h', 'v'))
            if can_place(word, r, c, dir):
                place_sparse(word, r, c, dir)
                placed.append({'word': word, 'x': c, 'y': r, 'dir': dir, 'question': question, 'number': next_number,
                               'type': 'normal'})
                next_number += 1
                placed_try = True
                break
        if not placed_try:
            placed.append({'word': word, 'x': None, 'y': None, 'dir': 'skipped', 'question': question, 'number': None,
                           'type': 'skipped'})

    if not sparse:
        return {'grid': [], 'placed': [], 'rows': 0, 'cols': 0}

    rows = [p[0] for p in sparse.keys()]
    cols = [p[1] for p in sparse.keys()]
    rmin, rmax, cmin, cmax = min(rows), max(rows), min(cols), max(cols)
    R = rmax - rmin + 1
    C = cmax - cmin + 1
    dense = [['' for _ in range(C)] for _ in range(R)]
    for (rr, cc), ch in sparse.items():
        dense[rr - rmin][cc - cmin] = ch

    placed_norm = []
    for p in placed:
        if p['x'] is None or p['y'] is None:
            placed_norm.append({'word': p['word'], 'x': None, 'y': None, 'dir': p['dir'], 'question': p['question'],
                                'number': p['number'], 'type': p['type']})
        else:
            placed_norm.append(
                {'word': p['word'], 'x': p['x'] - cmin, 'y': p['y'] - rmin, 'dir': p['dir'], 'question': p['question'],
                 'number': p['number'], 'type': p['type']})

    return {'grid': dense, 'placed': placed_norm, 'rows': R, 'cols': C}
