"""Own only this assistant and its descendants; no daemon or model service."""
import os
import uuid

_job = None


def bind_assistant_lifetime():
    """Windows inherits job membership, not this private non-inheritable handle."""
    global _job
    if _job is not None:
        return
    import win32api
    import win32job
    job = win32job.CreateJobObject(None, 'Local\\BD2Assistant_' + uuid.uuid4().hex)
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    try:
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    except Exception:
        job.Close()
        raise RuntimeError('无法绑定助手进程退出回收，未启动任务或模型。')
    _job = job


def membership():
    import win32api
    import win32job
    return {'gui_pid':os.getpid(), 'bound':_job is not None,
            'in_own_job':bool(_job and win32job.IsProcessInJob(win32api.GetCurrentProcess(), _job)),
            'kill_on_job_close':bool(_job and win32job.QueryInformationJobObject(
                _job, win32job.JobObjectExtendedLimitInformation)['BasicLimitInformation']['LimitFlags']
                & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)}
