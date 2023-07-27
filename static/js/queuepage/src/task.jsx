'use strict';

class TaskPlot extends React.PureComponent {
    constructor(props) {
        super(props);
    }

    componentDidMount() {
        console.log('activating plot', this.props.taskid)
        const plot_url = new URL(this.props.taskurl);
        plot_url.pathname += 'resultplotdata.js';
        plot_url.search = '';
        $.ajax({ url: plot_url, cache: true, dataType: 'script' });
    }

    componentWillUnmount() {
        console.log('Unmounting plot for task ', this.props.taskid);
        delete jslimitsglobal['#plotforcedflux-task-' + this.props.taskid]
        delete jslcdataglobal['#plotforcedflux-task-' + this.props.taskid]
        delete jslabelsglobal['#plotforcedflux-task-' + this.props.taskid]
    }

    render() {
        return (
            <div key='plot' id={'plotforcedflux-task-' + this.props.taskid} className="plot" style={{ width: '100%', height: '300px' }}></div>
        );
    }
}

class Task extends React.Component {
    constructor(props) {
        super(props);
        this.state = {}
        this.state.updateTimeElapsed = this.updateTimeElapsed.bind(this);
        this.state.interval = null;
        this.state.timeelapsed = -1;
    }

    deleteTask() {
        const li_id = '#task-' + this.props.taskdata.id
        // $(li_id).hide(300);
        $(li_id).slideUp(200);
        setTimeout(() => {
            // console.log('Starting delete of task ', this.props.taskdata.id);
            $.ajax({
                headers: {
                    "X-CSRFToken": getCookie("csrftoken")
                },
                url: this.props.taskdata.url, method: 'delete',
                success: (result) => { console.log('Deleted task ', this.props.taskdata.id); this.props.fetchData() },
                error: (err) => { console.log('Failed to delete task ', this.props.taskdata.id, err); $('#task-' + this.props.taskdata.id).slideDown(100); this.props.fetchData(); }
            });
        }, 200);
    }

    requestImages() {
        const request_image_url = new URL(this.props.taskdata.url);
        request_image_url.pathname += 'requestimages';
        request_image_url.search = '';

        fetch(request_image_url,
            {
                credentials: "same-origin",
                method: "GET",
                headers: {
                    "X-CSRFToken": getCookie("csrftoken"),
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
            })
            .then((response) => {
                if (response.redirected) {
                    // console.log(response)
                    const newimgtask_id = parseInt(new URL(response.url).searchParams.get('newids'));
                    newtaskids.push(newimgtask_id);
                    console.log('requestimages created task', newimgtask_id);
                    const new_page_url = new URL(response.url);
                    new_page_url.searchParams.delete('newids');
                    window.history.pushState({}, document.title, new_page_url);
                    this.props.fetchData(true);
                }
            });
    }

    static getDerivedStateFromProps(props, state) {
        if (props.taskdata.starttimestamp != null && props.taskdata.finishtimestamp == null) {
            if (state.interval == null) {
                const starttime = new Date(props.taskdata.starttimestamp).getTime();
                const timeelapsed = (new Date().getTime() - starttime) / 1000.;
                return { 'interval': setInterval(state.updateTimeElapsed, 1000), 'timeelapsed': timeelapsed.toFixed(0) };
            }
        } else if (state.interval != null) {
            return { 'interval': null };
        }

        return null;
    }

    componentDidMount() {
        // componentDidUpdate() {
        this.updateTimeElapsed();
        if (newtaskids.includes(this.props.taskdata.id)) {
            const li_id = '#task-' + this.props.taskdata.id
            console.log('showing new task', this.props.taskdata.id);
            $(li_id).hide();
            // $(li_id).show(600);
            $(li_id).slideDown(200);
            newtaskids = newtaskids.filter(item => { return item !== this.props.taskdata.id })
        }

        // this.interval = setInterval(() => {this.updateTimeElapsed()}, 1000);
    }

    componentWillUnmount() {
        clearInterval(this.state.interval);
        // this.state.interval = null;
    }

    updateTimeElapsed() {
        if (this.props.taskdata.starttimestamp != null && this.props.taskdata.finishtimestamp == null) {
            const starttime = new Date(this.props.taskdata.starttimestamp).getTime();
            const timeelapsed = (new Date().getTime() - starttime) / 1000.;
            this.setState({ 'timeelapsed': timeelapsed.toFixed(0) });
        } else if (this.state.interval != null) {
            clearInterval(this.state.interval);
            this.setState({ 'interval': null });
        }
    }

    shouldComponentUpdate(nextProps, nextState) {
        if (nextProps.taskdata.starttimestamp != null && nextProps.taskdata.finishtimestamp == null) {
            return true;
        }
        if (JSON.stringify(nextProps) != JSON.stringify(this.props)) {
            return true;
        }

        return false;
    }

    render() {
        const task = this.props.taskdata;
        let statusclass = 'none';
        let buttontext = 'none';
        if (task.finishtimestamp != null) {
            statusclass = "finished";
            buttontext = 'Delete';
        } else if (task.starttimestamp != null) {
            statusclass = "queued started";
            buttontext = 'Cancel';
        } else {
            statusclass = "queued notstarted";
            buttontext = 'Cancel';
        }
        console.log('Task ' + task.id + ' rendered');
        let delbutton = null;
        if (task.user_id == user_id) {
            delbutton = <button className="btn btn-sm btn-danger" onClick={() => this.deleteTask()}>{buttontext}</button>;
        }
        let taskbox = [
            <div key="rightside" className="rightside">
                {delbutton}
                <img src={task.previewimage_url} style={{ display: 'block', marginTop: '1em', marginLeft: '1em' }} />
            </div>
        ];

        taskbox.push(<div key="tasknum"><a key="tasklink" href={task.url} onClick={(e) => { this.props.setSingleTaskView(e, task.id, task.url) }}>Task {task.id}</a></div>);

        if (task.parent_task_url) {
            taskbox.push(<p key="imgrequest">Image request for <a key="parent_task_link" href={task.parent_task_url} onClick={(e) => { this.props.setSingleTaskView(e, task.parent_task_id, task.parent_task_url) }}>Task {task.parent_task_id}</a></p>);
        } else if (task.parent_task_id) {
            taskbox.push(<p key="imgrequest">Image request for Task {task.parent_task_id} (deleted)</p>);
        } else if (task.request_type == 'IMGZIP') {
            taskbox.push(<p key="imgrequest">Image request</p>);
        }

        if (task.request_type == 'IMGZIP') {
            const imagetype = task.use_reduced ? 'reduced' : 'difference';
            taskbox.push(<p key="imgrequestnote">Up to the first 500 {imagetype} images will be retrieved. The image request and download link may expire after one week.</p>);
        }

        if (task.user_id != user_id) {
            taskbox.push(<div key="user">User: {task.username}</div>);
        }

        if (task.comment != null && task.comment != '') {
            taskbox.push(<div key="comment">Comment: <b>{task.comment}</b></div>);
        }

        if (task.mpc_name != null && task.mpc_name != '') {
            taskbox.push(<div key="target">MPC Object: {task.mpc_name}</div>);
        } else {
            let radecepoch = '';
            if (task.radec_epoch_year != null) {
                radecepoch = <span>(epoch {task.radec_epoch_year}) </span>;
            }
            taskbox.push(<div key="target">RA Dec: {radecepoch}{task.ra} {task.dec}</div>);
            if (task.propermotion_ra > 0 || task.propermotion_dec > 0) {
                taskbox.push(<div key="propermotion">Proper motion [mas/yr]: {task.propermotion_ra} {task.propermotion_dec}</div>);
            }
        }

        taskbox.push(<div key="imgtype">Images: {task.use_reduced ? 'Reduced' : 'Difference'}</div>);

        if (task.mjd_min != null || task.mjd_max != null) {
            const mjdmin = task.mjd_min != null ? task.mjd_min : "0";
            const mjdmax = task.mjd_max != null ? task.mjd_max : "∞";
            taskbox.push(<div key="mjdrange">MJD request: [{mjdmin}, {mjdmax}]</div>);
        }

        taskbox.push(<div key="queuetime">Queued at {new Date(task.timestamp).toLocaleString()}</div>);
        if (task.finishtimestamp != null) {
            taskbox.push(<div key="status">Finished at {new Date(task.finishtimestamp).toLocaleString()}</div>);
            if (task.error_msg != null) {
                taskbox.push(<p key="error_msg" style={{ color: 'black', fontWeight: 'bold', marginTop: '1em' }}>Error: {task.error_msg}</p>);
            } else {
                if (task.request_type == 'FP') {
                    taskbox.push(<a key="datalink" className="results btn btn-info getdata" href={task.result_url} target="_blank">Data</a>);
                    taskbox.push(<a key="pdflink" className="results btn btn-info getpdf" href={task.pdfplot_url} target="_blank">PDF</a>);
                }

                if (task.request_type == 'IMGZIP') {
                    if (task.result_imagezip_url != null) {
                        taskbox.push(<a key="imgdownload" className="results btn btn-info" href={task.result_imagezip_url}>Download images (ZIP)</a>);
                    } else {
                        taskbox.push(<p>The download link has expired. Delete this task and request again if necessary.</p>);
                    }
                } else if (task.imagerequest_task_id != null) {
                    if (task.imagerequest_finished) {
                        taskbox.push(<a key="imgrequest" className="btn btn-primary" href={task.imagerequest_url} onClick={(e) => { this.props.setSingleTaskView(e, task.imagerequest_task_id, task.imagerequest_url) }}>Images retrieved</a>);
                    } else {
                        taskbox.push(<a key="imgrequest" className="btn btn-warning" href={task.imagerequest_url} onClick={(e) => { this.props.setSingleTaskView(e, task.imagerequest_task_id, task.imagerequest_url) }}>Images requested</a>);
                    }
                } else if (user_id == task.user_id) {
                    taskbox.push(<button key="imgrequest" className="btn btn-info" onClick={() => this.requestImages()} title="Download FITS and JPEG images for up to the first 500 observations.">Request {task.use_reduced ? 'reduced' : 'diff'} images</button>);
                }
            }
        } else if (task.starttimestamp != null) {
            taskbox.push(<div key="status" style={{ color: 'red', fontStyle: 'italic', marginTop: '1em' }}>Running (started {this.state.timeelapsed} seconds ago)</div>);
        } else {
            taskbox.push(<div key="status" style={{ fontStyle: 'italic', marginTop: '1em' }}>Waiting ({task.queuepos} tasks ahead of this one)</div>);
        }

        if (task.finishtimestamp != null && task.error_msg == null && task.request_type == 'FP' && !this.props.hidePlot) {
            taskbox.push(<TaskPlot key='plot' taskid={task.id} taskurl={task.url} />);
        }

        return (
            <li key={"task-" + task.id} className={"task " + statusclass} id={"task-" + task.id}>
                {taskbox}
            </li>
        );
    }
}