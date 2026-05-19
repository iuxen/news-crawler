# 新闻爬虫脚本 (News Crawler)

本脚本用于自动抓取新闻并进行处理。

## 安装与配置

1.  克隆本仓库后，进入项目目录。
2.  本项目的真实配置文件 `config.json` 已被 `.gitignore` 忽略以保护隐私。
3.  **第一次运行时**，请复制示例配置文件并填入你自己的信息：
    ```bash
    cp config.example.json config.json
    ```
4.  打开 `config.json`，根据 `config.example.json` 的格式，填入你的真实API密钥等信息。
5.  （可选）根据代码需要，安装Python依赖项。

## 运行

在项目目录下执行以下命令：
```bash
python newscrawl202603.py
```