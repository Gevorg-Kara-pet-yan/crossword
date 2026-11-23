import random


def generate_crossword_grid(qa_list, keyword, attempts=400):
    """
    Վերադարձնում է dict { 'grid': trimmed_grid, 'placed': placed_words, 'rows': R, 'cols': C }
    qa_list: list of {'q': question, 'a': ANSWER_UPPER}
    keyword: string (already uppercased) կամ '' եթե ոչ

    ԿԱՐԵՎՈՐ․  բոլոր բառերը երաշխավորված ԿՊԱԾ (հատվող) են լինում իրար հետ։
    Ոչ մի բառ չի «թռչում» դատարկ տեղ։ Եթե ինչ-որ բառ ոչ մի կերպ չի հատվում,
    ապա այն ուղղակի բաց է թողնվում (skip), որպեսզի ցանցը չկտրվի։
    """

    if not qa_list and not keyword:
        return {'grid': [], 'placed': [], 'rows': 0, 'cols': 0, 'skipped': []}

    words = [{'word': qa['a'], 'question': qa.get('q', '')} for qa in qa_list]

    # ================================================================
    # 1) KEYWORD MODE — բոլոր բառերը հատվում են ուղղահայաց բանալի բառին
    # ================================================================
    if keyword:
        max_word_len = max((len(w['word']) for w in words), default=0)
        word_count = len(words)

        grid_height = max(len(keyword), word_count + 4) + 10
        grid_width = max(len(keyword), max_word_len + 5) + 10

        grid = [['' for _ in range(grid_width)] for _ in range(grid_height)]
        placed_words = []

        keyword_col = grid_width // 2
        keyword_row = (grid_height - len(keyword)) // 2
        for i, ch in enumerate(keyword):
            grid[keyword_row + i][keyword_col] = ch

        placed_words.append({
            'word': keyword, 'x': keyword_col, 'y': keyword_row,
            'dir': 'v', 'question': 'Բանալի բառ', 'number': None, 'type': 'keyword'
        })

        # ── Max matching՝ բառ ↔ բանալի-տող ─────────────────────────
        match_row = _match_words_to_keyword(keyword, words)
        matched = set(wi for wi in match_row if wi != -1)

        clue_number = 1
        for p, wi in enumerate(match_row):
            if wi == -1:
                continue
            word = words[wi]['word']
            question = words[wi]['question']

            # Ընտրում ենք keyword[p]-ին համապատասխան այն տառը, որ ցանցում տեղավորվի
            placed_li = None
            for li, ch in enumerate(word):
                sc = keyword_col - li
                if ch == keyword[p] and 0 <= sc and sc + len(word) <= grid_width:
                    placed_li = li
                    break
            if placed_li is None:
                matched.discard(wi)  # չտեղավորվեց → skip
                continue

            start_row = keyword_row + p
            start_col = keyword_col - placed_li
            for j, ch in enumerate(word):
                grid[start_row][start_col + j] = ch
            placed_words.append({
                'word': word, 'x': start_col, 'y': start_row, 'dir': 'h',
                'question': question, 'number': clue_number, 'type': 'normal'
            })
            clue_number += 1

        # Չհամընկածները՝ skip
        for idx, w in enumerate(words):
            if idx not in matched:
                placed_words.append({
                    'word': w['word'], 'x': None, 'y': None, 'dir': 'skipped',
                    'question': w['question'], 'number': None, 'type': 'skipped'
                })

        return _trim_and_pack(grid, grid_height, grid_width, placed_words, keyword_col)

    # ================================================================
    # 2) NO-KEYWORD MODE — սովորական հատվող խաչբառ (միայն կպած բառեր)
    # ================================================================
    def build_once(order):
        sparse = {}
        placed = []
        number = 1

        def can_place(word, r, c, direction):
            """Վերադարձնում է (թույլատրելի՞, հատվում-է՞)"""
            dr, dc = (0, 1) if direction == 'h' else (1, 0)
            touches = False
            for i, ch in enumerate(word):
                rr, cc = r + dr * i, c + dc * i
                cur = sparse.get((rr, cc))
                if cur is None:
                    # ազատ վանդակ — կողքից (ուղղահայաց) հարևան չլինի
                    if direction == 'h':
                        if sparse.get((rr - 1, cc)) is not None or sparse.get((rr + 1, cc)) is not None:
                            return False, False
                    else:
                        if sparse.get((rr, cc - 1)) is not None or sparse.get((rr, cc + 1)) is not None:
                            return False, False
                else:
                    # լցված վանդակ — պիտի ճիշտ նույն տառը լինի (հատում)
                    if cur != ch:
                        return False, False
                    touches = True
            # Բառի ծայրերին կպած տառ չլինի
            before = (r - dr, c - dc)
            after = (r + dr * len(word), c + dc * len(word))
            if sparse.get(before) is not None or sparse.get(after) is not None:
                return False, False
            return True, touches

        def place(word, r, c, direction):
            dr, dc = (0, 1) if direction == 'h' else (1, 0)
            for i, ch in enumerate(word):
                sparse[(r + dr * i, c + dc * i)] = ch

        # Առաջին (ամենաերկար) բառը դնում ենք որպես հենք
        first = order[0]
        place(first['word'], 0, 0, 'h')
        placed.append({'word': first['word'], 'x': 0, 'y': 0, 'dir': 'h',
                       'question': first['question'], 'number': number, 'type': 'normal'})
        number += 1

        remaining = order[1:]
        # Մի քանի անցում՝ ուշ դրված բառը կարող է հատում ստեղծել շուտ չտեղավորվածի համար
        progress = True
        while progress and remaining:
            progress = False
            still = []
            for w in remaining:
                word = w['word']
                # Թեկնածու դիրքեր՝ ՄԻԱՅՆ արդեն դրված տառերի հետ հատումից
                candidates = []
                for wi, ch in enumerate(word):
                    for (rr, cc), gch in sparse.items():
                        if gch == ch:
                            candidates.append(('h', rr, cc - wi))
                            candidates.append(('v', rr - wi, cc))
                random.shuffle(candidates)

                done = False
                for direction, rs, cs in candidates:
                    ok, touches = can_place(word, rs, cs, direction)
                    if ok and touches:  # պարտադիր հատում
                        place(word, rs, cs, direction)
                        placed.append({'word': word, 'x': cs, 'y': rs, 'dir': direction,
                                       'question': w['question'], 'number': number, 'type': 'normal'})
                        number += 1
                        progress = True
                        done = True
                        break
                if not done:
                    still.append(w)
            remaining = still

        placed_count = len(placed)
        return sparse, placed, placed_count

    words_sorted = sorted(words, key=lambda x: len(x['word']), reverse=True)

    best = None
    for _ in range(attempts):
        # Ամեն փորձի ժամանակ ՓՈԽՈՒՄ ենք խարիսխը (առաջին բառը),
        # որպեսզի պահվի ամենամեծ ԿԱՊԱԿՑՎԱԾ խումբը, ոչ թե պատահական անջատ բառը։
        pool = words_sorted[:]
        random.shuffle(pool)
        anchor = pool[0]
        rest = sorted(pool[1:], key=lambda x: len(x['word']), reverse=True)
        order = [anchor] + rest

        sparse, placed, cnt = build_once(order)
        if best is None or cnt > best[0]:
            best = (cnt, sparse, placed)
        if cnt == len(words):  # բոլորը տեղավորվեցին՝ ավելի լավ չի լինի
            break

    _, sparse, placed = best

    if not sparse:
        return {'grid': [], 'placed': [], 'rows': 0, 'cols': 0,
                'skipped': [{'word': w['word'], 'question': w['question']} for w in words]}

    # Խիտ ցանց + նորմալիզացիա
    rs = [p[0] for p in sparse.keys()]
    cs = [p[1] for p in sparse.keys()]
    rmin, rmax, cmin, cmax = min(rs), max(rs), min(cs), max(cs)
    R = rmax - rmin + 1
    C = cmax - cmin + 1
    dense = [['' for _ in range(C)] for _ in range(R)]
    for (rr, cc), ch in sparse.items():
        dense[rr - rmin][cc - cmin] = ch

    placed_norm = []
    for p in placed:
        placed_norm.append({
            'word': p['word'], 'x': p['x'] - cmin, 'y': p['y'] - rmin,
            'dir': p['dir'], 'question': p['question'],
            'number': p['number'], 'type': p['type']
        })

    # Որ բառերը չտեղավորվեցին (չհատվեցին) — ծանուցելու համար
    placed_answers = {p['word'] for p in placed_norm}
    skipped = [
        {'word': w['word'], 'question': w['question']}
        for w in words
        if w['word'] not in placed_answers
    ]

    return {'grid': dense, 'placed': placed_norm, 'rows': R, 'cols': C,
            'skipped': skipped}


def _trim_and_pack(grid, grid_height, grid_width, placed_words, keyword_col):
    """Keyword-mode-ի համար՝ կտրում է դատարկ եզրերը և վերադարձնում պատրաստ dict"""
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

    trimmed = [[grid[r][c] for c in range(min_col, max_col + 1)]
               for r in range(min_row, max_row + 1)]

    # Միայն իրական (տեղավորված) բառերը՝ առանց skip-երի
    result_placed = []
    skipped = []
    for p in placed_words:
        if p.get('type') == 'skipped' or p['x'] is None or p['y'] is None:
            if p.get('type') != 'keyword':
                skipped.append({'word': p['word'], 'question': p.get('question', '')})
            continue
        p2 = dict(p)
        p2['x'] -= min_col
        p2['y'] -= min_row
        result_placed.append(p2)

    return {
        'grid': trimmed,
        'placed': result_placed,
        'rows': len(trimmed),
        'cols': len(trimmed[0]) if trimmed else 0,
        'skipped': skipped
    }
def _match_words_to_keyword(keyword, words):
    """Max bipartite matching: word i կարող է գնալ row p, եթե keyword[p] կա word-ում։
    Վերադարձնում է match_row: row_index -> word_index (կամ -1)։"""
    n_rows = len(keyword)
    adj = []
    for w in words:
        letters = set(w['word'])
        adj.append([p for p in range(n_rows) if keyword[p] in letters])

    match_row = [-1] * n_rows  # row -> word idx

    def try_assign(wi, seen):
        for p in adj[wi]:
            if not seen[p]:
                seen[p] = True
                if match_row[p] == -1 or try_assign(match_row[p], seen):
                    match_row[p] = wi
                    return True
        return False

    # Սկսում ենք ամենասահմանափակ բառերից (ամենաքիչ տարբերակ ունեցողներից)
    order = sorted(range(len(words)), key=lambda i: len(adj[i]))
    for wi in order:
        try_assign(wi, [False] * n_rows)
    return match_row