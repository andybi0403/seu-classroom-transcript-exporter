# 东大云课堂语音导出工具

在 Windows 上把东大云课堂的字幕导出为按周整理的 Word 和带时间戳的 JSON。工具优先使用平台字幕。没有字幕的课次可以从回放提取教师音轨，在本机识别并补上标点。

## 安装

1. 下载或克隆本仓库。在 Windows 上安装 [Python 3.12 或 3.14 x64](https://www.python.org/downloads/windows/)，确保命令行可运行 `py -3 --version`。
2. 需要识别无字幕回放或本地录音时，联网运行 `准备离线转写.cmd`。脚本会在项目内安装独立运行环境并下载模型。有可用 NVIDIA GPU 时优先使用 CUDA。模型下载完成后，识别和补标点可离线运行。
3. 双击 `启动课堂语音导出工具.cmd`，浏览器会打开 [本机导出页面](http://127.0.0.1:8786)。导出期间保留启动窗口。

## 导出课程

1. 连接校园网或校园 VPN，在 Chrome 或 Edge 登录东大云课堂并打开点播课程。
2. 复制地址栏中带 `play-center` 的完整链接，粘贴到工具中。可先点「预览课程」核对课次，再选择保存位置并开始导出。
3. 默认会为缺字幕且有回放的课次补录。音轨提取与当前课次的转写并行，生成的临时音频会自动清理。进度和未补成的课次会显示在页面上。

导出中断后，用同一个课程链接，选回原来的输出文件夹。工具会复用同一教学班已保存的本地转写。

## 转写本地音视频

选择「本地音视频转写」，粘贴音视频文件的完整路径，填写课程名和周次。可选同一周的多个文件，每行一个路径。已有 `seu_voice_raw.json` 时，可以展开「补字幕到以前的导出记录」，把文件分配给指定的无字幕课次。

## 导出内容

- `第XX周_课堂语音原话.docx` 按课次整理，采用简体中文和微软雅黑。
- `seu_voice_raw.json` 的 `rawSegments` 保存字幕片段和时间戳，`punctuatedText` 保存本地转写补标点后的阅读版。

课程点播读取本机 Chrome 或 Edge 中已有的云课堂登录状态。录音和字幕在本机处理。请通过自己的账号操作有访问权限的课程，并将课程资料留在本地。

## 致谢

- **原工具署名** Analoop
- **Word 文档生成器署名** HZL不凉

本地转写和标点恢复使用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 和 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)。东大点播接口参考了 [SEUdaily 的课程接口记录](https://github.com/miunerofrade/SEUdaily/blob/1e9d1e7dbee14ec8282448c49cb63ba1dd815e71/docs/research/seu-course-http.md)。

## 许可证

本项目源码采用 [MIT 许可证](LICENSE)。
