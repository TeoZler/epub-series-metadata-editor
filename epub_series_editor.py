#!/usr/bin/env python3
import argparse,os,zipfile,xml.etree.ElementTree as ET,shutil,pathlib,sys
import re,random,math
CALIBRE_NS="http://calibre.kovidgoyal.net/2009/metadata"
ET.register_namespace("calibre", CALIBRE_NS)

def sanitize_opf_xml(data_bytes):
    # 针对某些 OPF 在 <package>/<metadata> 标签中重复声明属性（例如重复的 xmlns:opf），进行一次性去重
    try:
        s=data_bytes.decode('utf-8')
    except UnicodeDecodeError:
        s=data_bytes.decode('utf-8', errors='replace')
    def dedupe_tag(tag_name, s):
        pattern=rf'<((?:[A-Za-z_][\w\-]*:)?{tag_name})\b([^>]*)>'
        def repl(m):
            qname=m.group(1)
            attrs=m.group(2)
            parts=list(re.finditer(r'([^\s=]+)\s*=\s*("[^"]*"|\'[^\']*\')', attrs))
            seen=set(); out=[]
            for mm in parts:
                name=mm.group(1); val=mm.group(2)
                if name not in seen:
                    seen.add(name)
                    out.append(f"{name}={val}")
            new_attrs=(' ' + ' '.join(out)) if out else ''
            return f'<{qname}{new_attrs}>'
        return re.sub(pattern, repl, s, flags=re.IGNORECASE)
    s=dedupe_tag('package', s)
    s=dedupe_tag('metadata', s)
    return s.encode('utf-8')
def find_opf(z):
    try:
        data=z.read("META-INF/container.xml")
        r=ET.fromstring(data).find(".//{*}rootfile")
        return r.get("full-path")
    except Exception:
        for name in z.namelist():
            if name.lower().endswith(".opf"): return name
        raise RuntimeError("未找到OPF")
def parse_opf(data):
    try:
        root=ET.fromstring(data)
    except ET.ParseError:
        # 回退：清理开头标签可能重复的属性（如重复的 xmlns: 前缀声明）后重试
        data=sanitize_opf_xml(data)
        root=ET.fromstring(data)
    meta=root.find(".//{*}metadata")
    if meta is None: raise RuntimeError("OPF缺少metadata")
    return root,meta
def get_series(meta):
    for e in meta.findall(".//{*}meta"):
        prop=e.get("property")
        if prop and prop.lower()=="belongs-to-collection":
            return e.text or ""
    for e in list(meta):
        if e.tag.endswith("series") and CALIBRE_NS in e.tag: return e.text or ""
    for e in meta.findall(".//{*}meta"):
        if (e.get("name")=="calibre:series") or (e.get("property")=="calibre:series"):
            return e.get("content") or (e.text or "")
    return None
def write_epub(epub_path,opf_path,new_opf,backup=True,backup_dir=None,backup_base=None):
    tmp=epub_path+".tmp"
    with zipfile.ZipFile(epub_path,"r") as zr, zipfile.ZipFile(tmp,"w",compression=zipfile.ZIP_DEFLATED) as zw:
        for it in zr.infolist():
            data=zr.read(it.filename)
            if it.filename==opf_path: data=new_opf
            zw.writestr(it,data)
    if backup:
        dest=epub_path+".bak"
        if backup_dir:
            try:
                base=pathlib.Path(backup_base).resolve() if backup_base else pathlib.Path(epub_path).resolve().parent
            except Exception:
                base=pathlib.Path(epub_path).resolve().parent
            p_epub=pathlib.Path(epub_path).resolve()
            try:
                rel=p_epub.relative_to(base)
            except Exception:
                rel=p_epub.name
            dest_path=pathlib.Path(backup_dir).resolve()/rel
            os.makedirs(str(dest_path.parent),exist_ok=True)
            dest=str(dest_path)+".bak" if not str(dest_path).endswith(".bak") else str(dest_path)
        shutil.copy2(epub_path,dest)
    os.replace(tmp,epub_path)
POLICY_FORCE_ALL=False
def process_file(path,series=None,index=None,force=False,skip=False,dry=False,backup=True,backup_dir=None,backup_base=None,write_collection=True,write_calibre=False):
    global POLICY_FORCE_ALL
    with zipfile.ZipFile(path,"r") as z:
        opf=find_opf(z); data=z.read(opf)
    root,meta=parse_opf(data)
    val=series or pathlib.Path(path).parent.name
    old=get_series(meta)
    if old and skip: return f"跳过(已有): {path}"
    if old and not (force or POLICY_FORCE_ALL):
        print(f"提示: {path} 已有系列: {old}")
        ans=input(f"是否替换为 '{val}'? [y/N/a/skip]: ").strip().lower()
        if ans=="skip": return f"跳过(用户): {path}"
        if ans=="a": POLICY_FORCE_ALL=True
        elif ans!="y": return f"跳过(用户): {path}"
    # 使用最小注入生成新的 OPF 内容，避免重序列化导致的其他改动
    new=inject_series_minimal(data, val, index, write_collection=write_collection, write_calibre=write_calibre)
    if dry: return f"预览: {path} -> {val}"
    write_epub(path,opf,new,backup,backup_dir,backup_base)
    return f"完成: {path} -> {val}"
def find_epubs(p,rec=False):
    p=pathlib.Path(p)
    if p.is_file() and p.suffix.lower()==".epub": return [str(p)]
    glob="**/*.epub" if rec else "*.epub"
    return [str(x) for x in p.glob(glob)]

def ask_yn(prompt, default=False):
    try:
        s=input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        s=""
    if not s:
        return default
    return s in ("y","yes")

def ask_choice(prompt, choices, default):
    try:
        s=input(prompt).strip().lower()
    except EOFError:
        s=""
    if not s:
        s=default
    while s not in choices:
        try:
            s=input(f"请输入 {','.join(sorted(choices))}，默认 {default}: ").strip().lower()
        except EOFError:
            s=default
        if not s:
            s=default
    return s

# 交互：按当前文件顺序分配/调整系列序号
# 命令：
#  - m i pos   将第 i 项移动到位置 pos
#  - s i j     交换第 i 与第 j 项
#  - set i N   将第 i 项的序号设为 N（支持整数或小数）
#  - start N   设置起始序号并按当前排序重新编号
#  - auto      按当前排序从起始序号自动连续编号
#  - done      完成并继续
#  - help      显示帮助与当前列表

def interactive_order_indices(flist, start=1):
    items = [{"file": f, "name": pathlib.Path(f).name, "idx": None} for f in flist]
    try:
        start = int(start)
    except Exception:
        start = 1
    cur = start
    for it in items:
        it["idx"] = cur
        cur += 1
    try:
        import msvcrt, os
        cursor = 0
        dragging = False
        def show():
            try:
                os.system("cls")
            except Exception:
                pass
            print("交互排序：↑/↓ 移动，回车切换拖动/浏览，S/N/A/C 序号操作，ESC 完成")
            print(f"模式：{'拖动' if dragging else '浏览'}，起始序号：{start}")
            for pos, it in enumerate(items, 1):
                prefix = ">" if pos-1 == cursor else " "
                print(f"{prefix} {pos:>2}. [{it['idx']}] {it['name']}")
        show()
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x1b",):
                break
            if ch in ("\r", "\n"):
                dragging = not dragging
                show()
                continue
            if ch in ("s","S"):
                try:
                    v = input("起始序号: ").strip()
                    start = float(v) if "." in v else int(v)
                except Exception:
                    start = start
                show()
                continue
            if ch in ("n","N"):
                try:
                    v = input("当前项序号: ").strip()
                    items[cursor]["idx"] = float(v) if "." in v else int(v)
                except Exception:
                    pass
                show()
                continue
            if ch in ("a","A"):
                cur = start
                for it in items:
                    it["idx"] = cur
                    cur = cur + 1
                show()
                continue
            if ch in ("c","C"):
                cur0 = items[cursor]["idx"]
                cur = (math.ceil(cur0) if isinstance(cur0, float) and not float(cur0).is_integer() else (cur0 + 1))
                items[cursor]["idx"] = cur0
                for i in range(cursor + 1, len(items)):
                    items[i]["idx"] = cur
                    cur = cur + 1
                show()
                continue
            if ch in ("\x00", "\xe0"):
                code = ord(msvcrt.getwch())
                if code == 72:
                    if dragging and cursor > 0:
                        it = items.pop(cursor)
                        cursor -= 1
                        items.insert(cursor, it)
                        show()
                    elif not dragging and cursor > 0:
                        cursor -= 1
                        show()
                elif code == 80:
                    if dragging and cursor < len(items)-1:
                        it = items.pop(cursor)
                        cursor += 1
                        items.insert(cursor, it)
                        show()
                    elif not dragging and cursor < len(items)-1:
                        cursor += 1
                        show()
                continue
        order = [it["file"] for it in items]
        idx_map = {it["file"]: it["idx"] for it in items}
        return order, idx_map
    except Exception:
        try:
            import curses
            def run(stdscr):
                curses.curs_set(0)
                cursor = 0
                dragging = False
                def draw():
                    stdscr.clear()
                    stdscr.addstr(0, 0, f"交互排序：↑/↓ 移动，回车切换拖动/浏览，S/N/A/C 序号操作，ESC 完成")
                    stdscr.addstr(1, 0, f"模式：{'拖动' if dragging else '浏览'}，起始序号：{start}")
                    for pos, it in enumerate(items, 1):
                        prefix = ">" if pos-1 == cursor else " "
                        stdscr.addstr(1+pos, 0, f"{prefix} {pos:>2}. [{it['idx']}] {it['name']}")
                    stdscr.refresh()
                draw()
                while True:
                    ch = stdscr.getch()
                    if ch == 27:
                        break
                    if ch in (10, 13):
                        dragging = not dragging
                        draw()
                        continue
                    if ch in (ord('s'), ord('S')):
                        curses.echo()
                        stdscr.addstr(len(items)+3, 0, "起始序号: ")
                        v = stdscr.getstr(len(items)+3, 6+4).decode('utf-8').strip()
                        curses.noecho()
                        try:
                            start = float(v) if "." in v else int(v)
                        except Exception:
                            pass
                        draw()
                        continue
                    if ch in (ord('n'), ord('N')):
                        curses.echo()
                        stdscr.addstr(len(items)+3, 0, "当前项序号: ")
                        v = stdscr.getstr(len(items)+3, 6+6).decode('utf-8').strip()
                        curses.noecho()
                        try:
                            items[cursor]["idx"] = float(v) if "." in v else int(v)
                        except Exception:
                            pass
                        draw()
                        continue
                    if ch in (ord('a'), ord('A')):
                        cur = start
                        for it in items:
                            it["idx"] = cur
                            cur = cur + 1
                        draw()
                        continue
                    if ch in (ord('c'), ord('C')):
                        cur0 = items[cursor]["idx"]
                        cur = (math.ceil(cur0) if isinstance(cur0, float) and not float(cur0).is_integer() else (cur0 + 1))
                        items[cursor]["idx"] = cur0
                        for i in range(cursor + 1, len(items)):
                            items[i]["idx"] = cur
                            cur = cur + 1
                        draw()
                        continue
                    if ch == curses.KEY_UP:
                        if dragging and cursor > 0:
                            it = items.pop(cursor)
                            cursor -= 1
                            items.insert(cursor, it)
                            draw()
                        elif not dragging and cursor > 0:
                            cursor -= 1
                            draw()
                    elif ch == curses.KEY_DOWN:
                        if dragging and cursor < len(items)-1:
                            it = items.pop(cursor)
                            cursor += 1
                            items.insert(cursor, it)
                            draw()
                        elif not dragging and cursor < len(items)-1:
                            cursor += 1
                            draw()
                return [it["file"] for it in items], {it["file"]: it["idx"] for it in items}
            order, idx_map = curses.wrapper(run)
            return order, idx_map
        except Exception:
            def show():
                print("\n当前排序与序号：")
                for pos, it in enumerate(items, 1):
                    print(f"  {pos:>2}. [{it['idx']}] {it['name']}")
                print("命令: m i pos | s i j | set i N | start N | auto | c i | done | help")
            show()
            while True:
                try:
                    cmd = input("输入命令(回车=done): ").strip().lower()
                except EOFError:
                    cmd = ""
                if not cmd or cmd in {"done", "go"}:
                    break
                if cmd == "help":
                    print("说明:")
                    print("  m i pos   将第 i 项移动到位置 pos")
                    print("  s i j     交换第 i 与第 j 项")
                    print("  set i N   将第 i 项的序号设为 N")
                    print("  start N   设置起始序号")
                    print("  auto      从起始序号自动连续编号")
                    print("  c i       从第 i 项当前序号开始向后连续编号")
                    show()
                    continue
                parts = cmd.split()
                try:
                    if parts[0] == "m" and len(parts) == 3:
                        i = int(parts[1]); pos = int(parts[2])
                        if not (1 <= i <= len(items) and 1 <= pos <= len(items)):
                            print("范围无效"); continue
                        it = items.pop(i-1)
                        items.insert(pos-1, it)
                        show()
                    elif parts[0] == "s" and len(parts) == 3:
                        i = int(parts[1]); j = int(parts[2])
                        if not (1 <= i <= len(items) and 1 <= j <= len(items)):
                            print("范围无效"); continue
                        items[i-1], items[j-1] = items[j-1], items[i-1]
                        show()
                    elif parts[0] == "set" and len(parts) == 3:
                        i = int(parts[1]); v = float(parts[2]) if "." in parts[2] else int(parts[2])
                        if not (1 <= i <= len(items)):
                            print("范围无效"); continue
                        items[i-1]["idx"] = v
                        show()
                    elif parts[0] == "start" and len(parts) == 2:
                        start = float(parts[1]) if "." in parts[1] else int(parts[1])
                        show()
                    elif parts[0] == "auto" and len(parts) == 1:
                        cur = start
                        for it in items:
                            it["idx"] = cur
                            cur = cur + 1
                        show()
                    elif parts[0] == "c" and len(parts) == 2:
                        i = int(parts[1])
                        if not (1 <= i <= len(items)):
                            print("范围无效"); continue
                        cur0 = items[i-1]["idx"]
                        cur = (math.ceil(cur0) if isinstance(cur0, float) and not float(cur0).is_integer() else (cur0 + 1))
                        items[i-1]["idx"] = cur0
                        for j in range(i, len(items)):
                            items[j]["idx"] = cur
                            cur = cur + 1
                        show()
                    else:
                        print("未知命令，输入 help 查看用法。")
                except Exception:
                    print("命令解析失败，请重试。")
            order = [it["file"] for it in items]
            idx_map = {it["file"]: it["idx"] for it in items}
            return order, idx_map

def interactive():
    print("交互模式：按提示配置处理选项")
    path = input("目标路径(文件或文件夹) [默认 .]: ").strip()
    if not path:
        path="."
    rec = ask_yn("递归处理子文件夹?", default=False)
    by_folder_first = ask_yn("是否按末级文件夹逐个确认系列策略?", default=True)
    wsel = ask_choice("写入标签类型 [1=EPUB3系列,2=Calibre系列,3=同时] (默认 1): ", {"1","2","3"}, "1")
    write_collection = (wsel in {"1","3"})
    write_calibre = (wsel in {"2","3"})
    series = None
    index = None
    dry = ask_yn("仅预览不更改文件?", default=False)
    backup = ask_yn("生成.bak备份?", default=True)
    # 新增：备份路径选择与基准路径确定
    backup_dir = None
    base_for_backup = str(pathlib.Path(path).resolve()) if pathlib.Path(path).is_dir() else str(pathlib.Path(path).resolve().parent)
    if backup:
        bsel = ask_choice("备份位置 [1=原文件夹,2=指定路径] (默认 1): ", {"1","2"}, "1")
        if bsel == "2":
            backup_dir = input("备份根路径: ").strip()
            if not backup_dir:
                print("提示：未输入路径，改为原文件夹备份。")
                backup_dir = None
            else:
                print(f"备份将保存到: {backup_dir}，并保留相对结构自: {base_for_backup}")
    files=find_epubs(path,rec)
    if not files:
        print("未找到EPUB文件"); return
    print(f"发现 {len(files)} 个EPUB")
    by_folder = by_folder_first
    ok=skip=err=0
    if by_folder:
        mode = "i"
        # 按父目录分组逐个决策
        groups = {}
        for f in sorted(files):
            d=str(pathlib.Path(f).parent)
            groups.setdefault(d, []).append(f)
        # 增加快速选项：da/ca/ia/sa 表示将当前选择应用到后续所有文件夹
        apply_to_all = False
        global_choice = None  # "d"/"c"/"i"/"s"
        global_ser = None     # 当 global_choice=="c" 时的统一系列名
        global_use_existing = False  # 是否启用优先沿用已有系列
        def has_series_and_value(fp):
            try:
                with zipfile.ZipFile(fp,"r") as z:
                    opf=find_opf(z); data=z.read(opf)
                _, meta = parse_opf(data)
                val = get_series(meta)
                return (val is not None and val!=""), val
            except Exception:
                return False, None
        def folder_series_counts(lst):
            cnt={}
            miss=0
            for f in lst:
                okv, v = has_series_and_value(f)
                if okv and v:
                    cnt[v]=cnt.get(v,0)+1
                else:
                    miss+=1
            return cnt, miss
        for d, flist in groups.items():
            dname = pathlib.Path(d).name
            print(f"\n文件夹: {d} (共 {len(flist)} 本)")
            if not apply_to_all:
                raw = input("选择策略 [d=父目录,c=统一自定义,i=逐本确认,s=跳过]，加 e=优先沿用已有系列(缺失项填充)，加a表示应用到后续 (默认 d): ").strip().lower()
                if not raw:
                    raw = "d"
                def parse_choice(s):
                    base = None
                    use_existing = ('e' in s)
                    apply_all = s.endswith("a")
                    for ch in ("d","c","i","s"):
                        if ch in s:
                            base = ch; break
                    if not base:
                        base = "d"
                    return base, use_existing, apply_all
                choice, use_existing, apply_to_all = parse_choice(raw)
                ser = None
                if apply_to_all:
                    global_choice = choice
                    global_use_existing = use_existing
                if choice == "c":
                    ser = input(f"输入该文件夹统一系列名(留空则使用'{dname}'): ").strip() or dname
                    if apply_to_all:
                        global_ser = ser
            else:
                choice = global_choice
                use_existing = global_use_existing
                ser = global_ser if choice == "c" else None
            if choice == "s":
                print(f"跳过文件夹: {d}")
                skip += len(flist)
                continue

            # 自动序号：按当前文件顺序分配，支持交互排序与修改
            enable_auto = ask_yn("按文件顺序自动分配系列序号?", default=True)
            final_list = flist
            indices_map = None
            if enable_auto:
                sraw = input("起始序号(默认 1): ").strip()
                try:
                    start_idx = int(sraw) if sraw else 1
                except Exception:
                    start_idx = 1
                final_list, indices_map = interactive_order_indices(flist, start_idx)

            folder_ser = None
            override_minority = False
            if use_existing:
                cnt, miss = folder_series_counts(final_list)
                if cnt:
                    if len(cnt) == 1:
                        folder_ser = next(iter(cnt.keys()))
                    else:
                        print("提示：该文件夹内已有系列不一致：")
                        for k,v in sorted(cnt.items(), key=lambda x: (-x[1], x[0])):
                            print(f"  {k}: {v} 本")
                        print(f"  无系列: {miss} 本")
                        pick = ask_choice("选择统一系列来源 [m=出现最多的系列,c=手动指定,d=父目录名] (默认 m): ", {"m","c","d"}, "m")
                        if pick == "m":
                            folder_ser = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[0][0]
                        elif pick == "c":
                            folder_ser = input(f"输入自选系列名(留空则使用'{dname}'): ").strip() or dname
                        else:
                            folder_ser = dname
                        override_minority = ask_yn("是否将其他不同系列统一为选定系列?", default=True)
                else:
                    folder_ser = None
            if folder_ser:
                for f in final_list:
                    try:
                        okv, v = has_series_and_value(f)
                        idx_use = (indices_map[f] if indices_map else None)
                        if okv:
                            if v == folder_ser:
                                if mode=='s':
                                    res=f"跳过(已有): {f}"
                                elif mode=='f':
                                    res=process_file(f,folder_ser,idx_use,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                                else:
                                    res=process_file(f,folder_ser,idx_use,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                            else:
                                if override_minority:
                                    res=process_file(f,folder_ser,idx_use,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                                else:
                                    res=f"跳过(保留不同系列): {f}"
                        else:
                            base_ser = ser if choice=="c" else (pathlib.Path(f).parent.name if choice=="d" else (input(f"文件: {pathlib.Path(f).name} 系列名(留空用父目录'{pathlib.Path(f).parent.name}'): ").strip() or pathlib.Path(f).parent.name if choice=="i" else None))
                            apply_ser = folder_ser if folder_ser else base_ser
                            if apply_ser is None:
                                res=f"跳过(无系列且策略为跳过): {f}"
                            else:
                                res=process_file(f,apply_ser,idx_use,force=(mode=='f'),skip=(mode=='s'),dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        print(res)
                        if res.startswith("完成"): ok+=1
                        elif res.startswith("跳过"): skip+=1
                    except Exception as e:
                        err+=1; print(f"错误: {f}: {e}")
            elif choice == "c":
                # 使用统一的系列名 ser
                for f in final_list:
                    try:
                        idx_use = (indices_map[f] if indices_map else None)
                        if mode=='f':
                            res=process_file(f,ser,idx_use,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        elif mode=='s':
                            res=process_file(f,ser,idx_use,force=False,skip=True,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        else:
                            res=process_file(f,ser,idx_use,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        print(res)
                        if res.startswith("完成"): ok+=1
                        elif res.startswith("跳过"): skip+=1
                    except Exception as e:
                        err+=1; print(f"错误: {f}: {e}")
            elif choice == "i":
                # 逐本确认系列名
                for f in final_list:
                    fname = pathlib.Path(f).name
                    ser_each = input(f"文件: {fname} 系列名(留空用父目录'{dname}'): ").strip() or dname
                    try:
                        idx_use = (indices_map[f] if indices_map else None)
                        if mode=='f':
                            res=process_file(f,ser_each,idx_use,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        elif mode=='s':
                            res=process_file(f,ser_each,idx_use,force=False,skip=True,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        else:
                            res=process_file(f,ser_each,idx_use,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        print(res)
                        if res.startswith("完成"): ok+=1
                        elif res.startswith("跳过"): skip+=1
                    except Exception as e:
                        err+=1; print(f"错误: {f}: {e}")
            else:
                # d = 使用父目录名
                for f in final_list:
                    try:
                        ser_d=dname
                        idx_use = (indices_map[f] if indices_map else None)
                        if mode=='f':
                            res=process_file(f,ser_d,idx_use,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        elif mode=='s':
                            res=process_file(f,ser_d,idx_use,force=False,skip=True,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        else:
                            res=process_file(f,ser_d,idx_use,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                        print(res)
                        if res.startswith("完成"): ok+=1
                        elif res.startswith("跳过"): skip+=1
                    except Exception as e:
                        err+=1; print(f"错误: {f}: {e}")
    else:
        # 不按文件夹时，直接按整体系列名处理，不写固定序号
        mode = ask_choice("遇到已有系列标签 [i=逐本确认,f=直接覆盖,s=跳过] (默认 i): ", {"i","f","s"}, "i")
        series = input("统一系列名(留空则使用父文件夹名): ").strip()
        print(f"系列名：{'每本父文件夹名' if not series else series}")
        print(f"处理模式：{('覆盖' if mode=='f' else '跳过' if mode=='s' else '逐本确认')}, 递归：{rec}, 预览：{dry}, 备份：{backup}")
        go=ask_yn("开始执行?", default=True)
        if not go:
            print("已取消"); return
        for f in files:
            try:
                if mode=='f':
                    res=process_file(f,series,None,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                elif mode=='s':
                    res=process_file(f,series,None,force=False,skip=True,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                else:
                    res=process_file(f,series,None,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup,write_collection=write_collection,write_calibre=write_calibre)
                print(res)
                if res.startswith("完成"): ok+=1
                elif res.startswith("跳过"): skip+=1
            except Exception as e:
                err+=1; print(f"错误: {f}: {e}")
    print(f"结果: 成功{ok}, 跳过{skip}, 错误{err}")

def main():
    # 无参数时默认进入交互模式
    if len(sys.argv) == 1:
        interactive(); return
    ap=argparse.ArgumentParser(description="批量为EPUB添加EPUB 3 belongs-to-collection系列标记，可选写入calibre系列标签")
    ap.add_argument("--path",default=".",help="目标文件或文件夹路径")
    ap.add_argument("--series",help="统一系列名(不指定则取父文件夹名)")
    ap.add_argument("--index",type=float,help="系列序号(可选)")
    ap.add_argument("--recursive",action="store_true",help="递归处理子文件夹")
    ap.add_argument("--force",action="store_true",help="不提示直接覆盖已有标签")
    ap.add_argument("--skip-existing",action="store_true",help="遇到已有标签时跳过")
    ap.add_argument("--dry-run",action="store_true",help="仅预览不更改文件")
    ap.add_argument("--no-backup",action="store_true",help="不生成.bak备份")
    ap.add_argument("--interactive","-i",action="store_true",help="进入交互模式")
    ap.add_argument("--backup-dir",help="将.bak备份保存到指定路径，并保留相对结构")
    ap.add_argument("--backup-base",help="备份相对结构的基准路径(默认为--path或文件的父目录)")
    # 自动序号相关
    ap.add_argument("--auto-index",action="store_true",help="按文件顺序自动分配系列序号")
    ap.add_argument("--auto-index-start",type=int,default=1,help="自动序号起始值(默认1)")
    ap.add_argument("--write-calibre",action="store_true",help="同时写入calibre:series与calibre:series_index")
    ap.add_argument("--no-collection",dest="write_collection",action="store_false",help="不写入belongs-to-collection与group-position")
    ap.set_defaults(write_collection=True)
    args=ap.parse_args()
    if args.interactive:
        interactive(); return
    files=find_epubs(args.path,args.recursive)
    if not files:
        print("未找到EPUB文件"); return
    files = sorted(files)
    print(f"待处理 {len(files)} 个EPUB")
    ok=skip=err=0
    base_for_backup = args.backup_base or (args.path if pathlib.Path(args.path).is_dir() else str(pathlib.Path(args.path).parent))
    indices_map = {f: args.auto_index_start + i for i, f in enumerate(files)} if args.auto_index else None
    for f in files:
        try:
            idx_use = (indices_map[f] if indices_map else args.index)
            res=process_file(f,args.series,idx_use,args.force,args.skip_existing,args.dry_run,backup=not args.no_backup,backup_dir=args.backup_dir,backup_base=base_for_backup,write_collection=args.write_collection,write_calibre=args.write_calibre)
            print(res)
            if res.startswith("完成"): ok+=1
            elif res.startswith("跳过"): skip+=1
        except Exception as e:
            err+=1; print(f"错误: {f}: {e}")
    print(f"结果: 成功{ok}, 跳过{skip}, 错误{err}")
def xml_escape(t):
    return t.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

# 在不改动其他现有内容的前提下，最小化注入系列标签
def inject_series_minimal(data_bytes, series, index=None, write_collection=True, write_calibre=False):
    try:
        s=data_bytes.decode('utf-8')
    except UnicodeDecodeError:
        s=data_bytes.decode('utf-8', errors='replace')
    # 匹配整个 metadata 片段，支持带前缀的标签
    meta_re=re.compile(r'(<(?P<prefix>[A-Za-z_][\w\-]*:)?metadata\b[^>]*>)(?P<body>.*?)(</(?P=prefix)?metadata>)', re.IGNORECASE|re.DOTALL)
    m=meta_re.search(s)
    if not m:
        raise RuntimeError("OPF缺少metadata")
    body=m.group('body')
    if write_calibre:
        body=re.sub(r'\s*<\s*calibre:series\b[^>]*>.*?</\s*calibre:series\s*>\s*', '', body, flags=re.IGNORECASE|re.DOTALL)
        body=re.sub(r'\s*<\s*calibre:series_index\b[^>]*>.*?</\s*calibre:series_index\s*>\s*', '', body, flags=re.IGNORECASE|re.DOTALL)
        body=re.sub(r'\s*<\s*meta\b[^>]*\bname\s*=\s*"(?:calibre:series|calibre:series_index)"[^>]*\/>\s*', '', body, flags=re.IGNORECASE)
        body=re.sub(r'\s*<\s*meta\b[^>]*\bproperty\s*=\s*"(?:calibre:series|calibre:series_index)"[^>]*>.*?</\s*meta\s*>\s*', '', body, flags=re.IGNORECASE|re.DOTALL)
    if write_collection:
        body=re.sub(r'\s*<\s*meta\b[^>]*\bproperty\s*=\s*"belongs-to-collection"[^>]*\/>\s*', '', body, flags=re.IGNORECASE)
        body=re.sub(r'\s*<\s*meta\b[^>]*\bproperty\s*=\s*"belongs-to-collection"[^>]*>.*?</\s*meta\s*>\s*', '', body, flags=re.IGNORECASE|re.DOTALL)
        body=re.sub(r'\s*<\s*meta\b[^>]*\bproperty\s*=\s*"(?:collection-type|group-position)"[^>]*\/>\s*', '', body, flags=re.IGNORECASE)
        body=re.sub(r'\s*<\s*meta\b[^>]*\bproperty\s*=\s*"(?:collection-type|group-position)"[^>]*>.*?</\s*meta\s*>\s*', '', body, flags=re.IGNORECASE|re.DOTALL)
    # 计算缩进（若存在换行，则取下一行的缩进；否则用两个空格）
    sample=body[:200]
    mi=re.match(r'\s*\n([ \t]*)', sample)
    indent=mi.group(1) if mi else '  '
    ins=""
    if write_collection:
        rid=f"col{random.randint(10000,99999)}"
        ins+=f"\n{indent}<meta property=\"belongs-to-collection\" id=\"{rid}\">{xml_escape(series)}</meta>"
        ins+=f"\n{indent}<meta refines=\"#{rid}\" property=\"collection-type\">series</meta>"
        if index is not None:
            ins+=f"\n{indent}<meta refines=\"#{rid}\" property=\"group-position\">{index}</meta>"
    if write_calibre:
        ins+=f"\n{indent}<meta name=\"calibre:series\" content=\"{xml_escape(series)}\" />"
        if index is not None:
            ins+=f"\n{indent}<meta name=\"calibre:series_index\" content=\"{index}\" />"
    # 如果原body不是以换行开始，则在插入片段后补一个换行，保证下一标签独立一行
    starts_nl = body.startswith('\n') or body.startswith('\r\n')
    post = '' if starts_nl else '\n'
    new_body=ins+post+body
    new_s=s[:m.start('body')] + new_body + s[m.end('body'):]
    return new_s.encode('utf-8')

if __name__=="__main__": main()
