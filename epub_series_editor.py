#!/usr/bin/env python3
import argparse,os,zipfile,xml.etree.ElementTree as ET,shutil,pathlib,sys
CALIBRE_NS="http://calibre.kovidgoyal.net/2009/metadata"
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
def remove_series(meta):
    for e in list(meta):
        if e.tag.endswith("series") and CALIBRE_NS in e.tag: meta.remove(e)
    for e in list(meta.findall(".//{*}meta")):
        if (e.get("name") in ("calibre:series","calibre:series_index")) or (e.get("property") in ("calibre:series","calibre:series_index")):
            meta.remove(e)
def add_series(root,meta,series,index=None,compat=False):
    if "xmlns:calibre" not in root.attrib: root.set("xmlns:calibre",CALIBRE_NS)
    e=ET.Element("{%s}series"%CALIBRE_NS); e.text=series; meta.append(e)
    if index is not None:
        ei=ET.Element("{%s}series_index"%CALIBRE_NS); ei.text=str(index); meta.append(ei)
    if compat:
        m=ET.Element("meta"); m.set("name","calibre:series"); m.set("content",series); meta.append(m)
        if index is not None:
            mi=ET.Element("meta"); mi.set("name","calibre:series_index"); mi.set("content",str(index)); meta.append(mi)
def write_epub(epub_path,opf_path,new_opf,backup=True):
    tmp=epub_path+".tmp"
    with zipfile.ZipFile(epub_path,"r") as zr, zipfile.ZipFile(tmp,"w",compression=zipfile.ZIP_DEFLATED) as zw:
        for it in zr.infolist():
            data=zr.read(it.filename)
            if it.filename==opf_path: data=new_opf
            zw.writestr(it,data)
    if backup: shutil.copy2(epub_path,epub_path+".bak")
    os.replace(tmp,epub_path)
POLICY_FORCE_ALL=False
def process_file(path,series=None,index=None,force=False,skip=False,dry=False,compat=False,backup=True):
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
    if old: remove_series(meta)
    add_series(root,meta,val,index,compat)
    new=ET.tostring(root,encoding="utf-8",xml_declaration=True)
    if dry: return f"预览: {path} -> {val}"
    write_epub(path,opf,new,backup)
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

def interactive():
    print("交互模式：按提示配置处理选项")
    path = input("目标路径(文件或文件夹) [默认 .]: ").strip()
    if not path:
        path="."
    rec = ask_yn("递归处理子文件夹?", default=False)
    series = input("统一系列名(留空则使用父文件夹名): ").strip()
    index = None
    inx = input("系列序号(留空跳过，支持小数): ").strip()
    if inx:
        try:
            index = float(inx)
        except Exception:
            print("警告：序号无效，已忽略。")
            index=None
    compat = ask_yn("同时写入兼容<meta>标签?", default=False)
    mode = ask_choice("遇到已有系列标签 [i=逐本确认,f=直接覆盖,s=跳过] (默认 i): ", {"i","f","s"}, "i")
    dry = ask_yn("仅预览不更改文件?", default=False)
    backup = ask_yn("生成.bak备份?", default=True)
    files=find_epubs(path,rec)
    if not files:
        print("未找到EPUB文件"); return
    print(f"发现 {len(files)} 个EPUB")
    print(f"系列名：{'每本父文件夹名' if not series else series}")
    if index is not None: print(f"系列序号：{index}")
    print(f"处理模式：{('覆盖' if mode=='f' else '跳过' if mode=='s' else '逐本确认')}, 递归：{rec}, 预览：{dry}, 备份：{backup}, 兼容meta：{compat}")
    go=ask_yn("开始执行?", default=True)
    if not go:
        print("已取消"); return
    ok=skip=err=0
    for f in files:
        try:
            if mode=='f':
                res=process_file(f,series,index,force=True,skip=False,dry=dry,compat=compat,backup=backup)
            elif mode=='s':
                res=process_file(f,series,index,force=False,skip=True,dry=dry,compat=compat,backup=backup)
            else:
                res=process_file(f,series,index,force=False,skip=False,dry=dry,compat=compat,backup=backup)
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
    ap=argparse.ArgumentParser(description="批量为EPUB添加<calibre:series>标签")
    ap.add_argument("--path",default=".",help="目标文件或文件夹路径")
    ap.add_argument("--series",help="统一系列名(不指定则取父文件夹名)")
    ap.add_argument("--index",type=float,help="系列序号(可选)")
    ap.add_argument("--recursive",action="store_true",help="递归处理子文件夹")
    ap.add_argument("--force",action="store_true",help="不提示直接覆盖已有标签")
    ap.add_argument("--skip-existing",action="store_true",help="遇到已有标签时跳过")
    ap.add_argument("--dry-run",action="store_true",help="仅预览不更改文件")
    ap.add_argument("--no-backup",action="store_true",help="不生成.bak备份")
    ap.add_argument("--compat-meta",action="store_true",help="同时写入<meta name='calibre:series'>")
    ap.add_argument("--interactive","-i",action="store_true",help="进入交互模式")
    args=ap.parse_args()
    if args.interactive:
        interactive(); return
    files=find_epubs(args.path,args.recursive)
    if not files:
        print("未找到EPUB文件"); return
    print(f"待处理 {len(files)} 个EPUB")
    ok=skip=err=0
    for f in files:
        try:
            res=process_file(f,args.series,args.index,args.force,args.skip_existing,args.dry_run,args.compat_meta,not args.no_backup)
            print(res)
            if res.startswith("完成"): ok+=1
            elif res.startswith("跳过"): skip+=1
        except Exception as e:
            err+=1; print(f"错误: {f}: {e}")
    print(f"结果: 成功{ok}, 跳过{skip}, 错误{err}")
if __name__=="__main__": main()