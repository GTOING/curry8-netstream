# Curry 本地适配补丁

`curry8-netstream-local.patch` 保存当前子仓库相对于 `0d1067c5ea557dac5f15cdd92268e21b6936f83b` 的本地适配，包括可取消接收与资源清理。原子仓库仍独立保留，本次不提交或推送其 origin。

仅对新克隆、已检出上述提交的干净目录运行：

```sh
git -C curry8-netstream apply --check ../patches/curry8-netstream-local.patch
git -C curry8-netstream apply ../patches/curry8-netstream-local.patch
uv sync --locked
```

已有工作目录不要重复应用补丁，也不要重置本地改动。补丁保存源码差异，不包含虚拟环境或采集数据。
