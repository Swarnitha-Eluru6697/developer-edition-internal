# Notebook runtime isolation (WS-RUNTIME-012)

User Python never runs inside a Workspace pod in this deployment:

- The in-process kernel is off by default (`local_python_kernel_enabled=False`). This chart also sets `LOCAL_PYTHON_KERNEL_ENABLED=false`. The app refuses to start outside `ENVIRONMENT=dev` if the kernel is enabled (`assert_local_kernel_safe_for_environment`, tested in `tests/test_startup_guards.py`).
- Cells go to the per-user kernel servers that JupyterHub spawns (`JUPYTERHUB_URL` / `jupyterhub.apiTokenSecretRef`).
- Every cell has a wall-clock limit of 600s. A cancel interrupts the kernel, and that kernel is shut down once its session ends or idles (lifecycle reconciler).

JupyterHub is deployed by its own chart (zero-to-jupyterhub, "z2jh"), so the per-kernel CPU, memory and network limits live **there**. The data plane's JupyterHub release must set at least the following:

```yaml
# z2jh values for the data plane's JupyterHub
singleuser:
  cpu:
    guarantee: 0.25
    limit: 2            # hard CPU cap per user server
  memory:
    guarantee: 512M
    limit: 4G           # OOM-killed beyond this, never the node
  storage:
    type: none          # no persistent home; work lives in Workspace files
  extraPodConfig:
    securityContext:
      runAsNonRoot: true
      seccompProfile: { type: RuntimeDefault }
  networkPolicy:
    enabled: true
    egressAllowRules:
      cloudMetadataServer: false    # never reach 169.254.169.254
      privateIPs: false             # no in-cluster / VPC services by default
      dnsPortsCloudMetadataServer: false
      dnsPortsKubeSystemNamespace: true
      nonPrivateIPs: true           # public package indexes; set false to air-gap
cull:
  enabled: true
  timeout: 3600         # stop idle servers after an hour
  every: 600
```

Release gate: before enabling notebooks for a tenant, confirm these values are present in that tenant's JupyterHub release (`helm get values <jupyterhub-release>`).
