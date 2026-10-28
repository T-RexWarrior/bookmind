# BookMind CLI

CLI 与网页共享后端引擎和运行数据。先在项目根目录完成源码安装：

```shell
python -m pip install -e .
python -m bookmind_cli --help
```

可通过 `bookmind --help` 查看当前命令。网页入口与教材学习流程见
[项目首页](../README.md)，运行目录、模型配置和开发方式见
[开发文档](../docs/development.md)。

`BOOKMIND_HOME` 指定运行目录；默认使用当前目录。使用 CLI 和 Web
访问同一份资料时，应使用同一运行目录。
