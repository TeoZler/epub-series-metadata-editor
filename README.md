# EPUB Series Metadata Editor

批量为EPUB添加`<calibre:series>`系列标签，默认取自EPUB所在路径的上一层文件夹名。支持单文件或整个文件夹处理、递归、覆盖策略与预览。

## 快速开始

- 交互模式（无参数默认进入）：
```powershell
python epub_series_editor.py
```
按提示选择目标路径、是否递归、系列名与覆盖策略。

- 命令行模式：
```powershell
python epub_series_editor.py --path "D:\Books\MySeries" --recursive
```

- 默认系列名：每个EPUB取其父文件夹名。
- 有已有标签时：交互模式或逐本提示；命令行可选一次性“a=全部替换”。
- 自动覆盖：加`--force`跳过提示；或加`--skip-existing`保留已有标签。
- 备份：默认生成`.bak`，可用`--no-backup`关闭。

## 常用示例

- 为单个EPUB设置统一系列：
```powershell
python epub_series_editor.py --path "D:\Books\A\book.epub" --series "My Series"
```

- 为整个文件夹统一系列名：
```powershell
python epub_series_editor.py --path "D:\Books\A" --series "My Series"
```

- 递归处理子文件夹，并仅预览：
```powershell
python epub_series_editor.py --path "D:\Books" --recursive --dry-run
```

- 写入系列序号，并兼容写入`<meta name="calibre:series">`：
```powershell
python epub_series_editor.py --path "D:\Books\A" --series "My Series" --index 1 --compat-meta
```

## 说明
- 解析`META-INF/container.xml`确定OPF路径；找不到则回退扫描`.opf`。
- 写入时确保根元素有`xmlns:calibre`，插入`<calibre:series>`（可选写入`<meta name="calibre:series">`）。
- 使用临时文件重建ZIP以安全替换OPF，防止损坏；默认生成`.bak`备份。

## 注意
- 某些非标准EPUB可能缺失`metadata`段或容器描述，脚本会提示错误并继续处理其它文件。
- Windows路径建议使用双引号或转义；大小写不敏感匹配`.epub`。