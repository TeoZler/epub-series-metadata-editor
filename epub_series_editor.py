#!/usr/bin/env python3
import argparse,os,zipfile,xml.etree.ElementTree as ET,shutil,pathlib,sys
import re
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
def process_file(path,series=None,index=None,force=False,skip=False,dry=False,backup=True,backup_dir=None,backup_base=None):
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
    new=inject_series_minimal(data, val, index)
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
    # 初始按传入顺序，序号从 start 连续编号
    try:
        start = int(start)
    except Exception:
        start = 1
    cur = start
    for it in items:
        it["idx"] = cur
        cur += 1
    
    def show():
        print("\n当前排序与序号：")
        for pos, it in enumerate(items, 1):
            print(f"  {pos:>2}. [{it['idx']}] {it['name']}")
        print("命令: m i pos | s i j | set i N | start N | auto | done | help")
    
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
            print("  start N   设置起始序号并按当前排序重新编号")
            print("  auto      按当前排序从起始序号自动连续编号")
            print("  done      完成并继续")
            show()
            continue
        parts = cmd.split()
        if not parts:
            show(); continue
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
                i = int(parts[1]); N = float(parts[2]) if "." in parts[2] else int(parts[2])
                if not (1 <= i <= len(items)):
                    print("范围无效"); continue
                items[i-1]["idx"] = N
                show()
            elif parts[0] == "start" and len(parts) == 2:
                start = int(parts[1])
                cur = start
                for it in items:
                    it["idx"] = cur
                    cur += 1
                show()
            elif parts[0] == "auto" and len(parts) == 1:
                cur = start
                for it in items:
                    it["idx"] = cur
                    cur += 1
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
    series = input("统一系列名(留空则使用父文件夹名): ").strip()
    # 交互模式不再提供“全局固定序号”或“文件夹固定序号”输入
    index = None
    mode = ask_choice("遇到已有系列标签 [i=逐本确认,f=直接覆盖,s=跳过] (默认 i): ", {"i","f","s"}, "i")
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
    by_folder = ask_yn("是否按末级文件夹逐个确认系列策略?", default=True)
    ok=skip=err=0
    if by_folder:
        # 按父目录分组逐个决策
        groups = {}
        for f in sorted(files):
            d=str(pathlib.Path(f).parent)
            groups.setdefault(d, []).append(f)
        # 增加快速选项：da/ca/ia/sa 表示将当前选择应用到后续所有文件夹
        apply_to_all = False
        global_choice = None  # "d"/"c"/"i"/"s"
        global_ser = None     # 当 global_choice=="c" 时的统一系列名
        for d, flist in groups.items():
            dname = pathlib.Path(d).name
            print(f"\n文件夹: {d} (共 {len(flist)} 本)")
            if not apply_to_all:
                raw = input("选择策略 [d=父目录,c=统一自定义,i=逐本确认,s=跳过]，加a表示应用到后续 (默认 d): ").strip().lower()
                if not raw:
                    raw = "d"
                while raw not in {"d","c","i","s","da","ca","ia","sa"}:
                    raw = (input("请输入 d/c/i/s 或 da/ca/ia/sa，默认 d: ").strip().lower() or "d")
                apply_to_all = raw.endswith("a")
                choice = raw[0]  # 基本策略标记
                ser = None
                if apply_to_all:
                    global_choice = choice
                if choice == "c":
                    ser = input(f"输入该文件夹统一系列名(留空则使用'{dname}'): ").strip() or dname
                    if apply_to_all:
                        global_ser = ser
            else:
                choice = global_choice
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

            if choice == "c":
                # 使用统一的系列名 ser
                for f in final_list:
                    try:
                        idx_use = (indices_map[f] if indices_map else None)
                        if mode=='f':
                            res=process_file(f,ser,idx_use,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                        elif mode=='s':
                            res=process_file(f,ser,idx_use,force=False,skip=True,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                        else:
                            res=process_file(f,ser,idx_use,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
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
                            res=process_file(f,ser_each,idx_use,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                        elif mode=='s':
                            res=process_file(f,ser_each,idx_use,force=False,skip=True,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                        else:
                            res=process_file(f,ser_each,idx_use,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
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
                            res=process_file(f,ser_d,idx_use,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                        elif mode=='s':
                            res=process_file(f,ser_d,idx_use,force=False,skip=True,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                        else:
                            res=process_file(f,ser_d,idx_use,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                        print(res)
                        if res.startswith("完成"): ok+=1
                        elif res.startswith("跳过"): skip+=1
                    except Exception as e:
                        err+=1; print(f"错误: {f}: {e}")
    else:
        # 不按文件夹时，直接按整体系列名处理，不写固定序号
        print(f"系列名：{'每本父文件夹名' if not series else series}")
        print(f"处理模式：{('覆盖' if mode=='f' else '跳过' if mode=='s' else '逐本确认')}, 递归：{rec}, 预览：{dry}, 备份：{backup}")
        go=ask_yn("开始执行?", default=True)
        if not go:
            print("已取消"); return
        for f in files:
            try:
                if mode=='f':
                    res=process_file(f,series,None,force=True,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                elif mode=='s':
                    res=process_file(f,series,None,force=False,skip=True,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
                else:
                    res=process_file(f,series,None,force=False,skip=False,dry=dry,backup=backup,backup_dir=backup_dir,backup_base=base_for_backup)
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
    ap=argparse.ArgumentParser(description="批量为EPUB添加<meta name='calibre:series'>标签")
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
            res=process_file(f,args.series,idx_use,args.force,args.skip_existing,args.dry_run,backup=not args.no_backup,backup_dir=args.backup_dir,backup_base=base_for_backup)
            print(res)
            if res.startswith("完成"): ok+=1
            elif res.startswith("跳过"): skip+=1
        except Exception as e:
            err+=1; print(f"错误: {f}: {e}")
    print(f"结果: 成功{ok}, 跳过{skip}, 错误{err}")
def xml_escape(t):
    return t.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

# 在不改动其他现有内容的前提下，最小化注入系列标签
def inject_series_minimal(data_bytes, series, index=None):
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
    # 移除已存在的 calibre 系列元素与<meta>，避免重复
    body=re.sub(r'\s*<\s*calibre:series\b[^>]*>.*?</\s*calibre:series\s*>\s*', '', body, flags=re.IGNORECASE|re.DOTALL)
    body=re.sub(r'\s*<\s*calibre:series_index\b[^>]*>.*?</\s*calibre:series_index\s*>\s*', '', body, flags=re.IGNORECASE|re.DOTALL)
    body=re.sub(r'\s*<\s*meta\b[^>]*\bname\s*=\s*"(?:calibre:series|calibre:series_index)"[^>]*\/>\s*', '', body, flags=re.IGNORECASE)
    body=re.sub(r'\s*<\s*meta\b[^>]*\bproperty\s*=\s*"(?:calibre:series|calibre:series_index)"[^>]*>.*?</\s*meta\s*>\s*', '', body, flags=re.IGNORECASE|re.DOTALL)
    # 计算缩进（若存在换行，则取下一行的缩进；否则用两个空格）
    sample=body[:200]
    mi=re.match(r'\s*\n([ \t]*)', sample)
    indent=mi.group(1) if mi else '  '
    # 仅注入<meta name="calibre:series" content="..." />格式，并确保后续内容换行
    ins=f"\n{indent}<meta name=\"calibre:series\" content=\"{xml_escape(series)}\" />"
    if index is not None:
        ins+=f"\n{indent}<meta name=\"calibre:series_index\" content=\"{index}\" />"
    # 如果原body不是以换行开始，则在插入片段后补一个换行，保证下一标签独立一行
    starts_nl = body.startswith('\n') or body.startswith('\r\n')
    post = '' if starts_nl else '\n'
    new_body=ins+post+body
    new_s=s[:m.start('body')] + new_body + s[m.end('body'):]
    return new_s.encode('utf-8')

if __name__=="__main__": main()