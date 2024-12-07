'use strict';

import React from "react"
import ReactDOM from 'react-dom';
import { NewRequest } from "newrequest";

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

export class Task extends React.Component {
    constructor(props) {
        super(props);
        this.state = {}
        this.state.updateTimeElapsed = this.updateTimeElapsed.bind(this);
        this.state.interval = null;
        this.state.timeelapsed = -1;
        this.state.httperror = '';
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
            .catch(error => {
                console.log('requestImages HTTP request failed', error);
                this.setState({ 'httperror': 'HTTP request failed.' });
            })
            .then((response) => {
                if (response.status == 200 && response.redirected) {
                    // console.log(response)
                    this.setState({ 'httperror': '' });
                    const newimgtask_id = parseInt(new URL(response.url).searchParams.get('newids'));
                    newtaskids.push(newimgtask_id);
                    console.log('requestimages created task', newimgtask_id);
                    const new_page_url = new URL(response.url);
                    new_page_url.searchParams.delete('newids');
                    window.history.pushState({}, document.title, new_page_url);
                    this.props.fetchData(true);
                } else {
                    response.json().then(data => {
                        console.log('requestImages: errors returned', data);
                        this.setState({ 'httperror': 'ERROR: ' + data["error"] });
                    });
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
        if (nextState.httperror != this.state.httperror) {
            return true;
        }
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
            taskbox.push(<p key="imgrequestnote">Up to the first 1000 {imagetype} images will be retrieved. The image request and download link may expire after one week.</p>);
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

        if (task.request_type == "SSOSTACK") {
            taskbox.push(<div key="imgtype">Image: Stacked</div>);
        } else {
            taskbox.push(<div key="imgtype">Images: {task.use_reduced ? 'Reduced' : 'Difference'}</div>);
        }

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
                } else if (task.request_type == 'SSOSTACK') {
                    taskbox.push(<a key="datalink" className="results btn btn-info getdata" href={task.result_url} target="_blank">Data</a>);
                    if (task.result_imagestack_url != null) {
                        taskbox.push(<a key="imgdownload" className="results btn btn-info" href={task.result_imagestack_url} target="_blank">Stacked image (FITS)</a>);
                    } else {
                        taskbox.push(<p>The download link has expired. Delete this task and request again if necessary.</p>);
                    }
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
                } else if (task.request_type == 'FP' && user_id == task.user_id) {
                    taskbox.push(<button key="imgrequest" className="btn btn-info" onClick={() => this.requestImages()} title="Download FITS and JPEG images for up to the first 1000 observations.">Request {task.use_reduced ? 'reduced' : 'diff'} images</button>);
                }
            }
        } else if (task.starttimestamp != null) {
            taskbox.push(<div key="status" style={{ color: 'red', fontStyle: 'italic', marginTop: '1em' }}>Running (started {this.state.timeelapsed} seconds ago)</div>);
        } else {
            taskbox.push(<div key="status" style={{ fontStyle: 'italic', marginTop: '1em' }}>Waiting ({task.queuepos} tasks ahead of this one)</div>);
        }

        if (this.state.httperror != '') {
            taskbox.push(<p key="httperror" style={{ 'color': 'red' }}>{this.state.httperror}</p>);
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

let tasklist_api_request_active = false;
const tasklist_fetchcache = [];
let tasklist_api_error = '';

class Pager extends React.PureComponent {
    constructor(props) {
        super(props);

        this.state = {}
        if (this.props.previous != null) {
            this.state.previous_cursor = new URL(this.props.previous).searchParams.get('cursor');
        }
        if (this.props.next != null) {
            this.state.next_cursor = new URL(this.props.next).searchParams.get('cursor');
        }
    }

    // componentWillReceiveProps(nextProps) {
    //   if (JSON.stringify(nextProps) != JSON.stringify(this.state))
    //   {
    //     this.setState(nextProps);
    //     console.log('Pager changed');
    //   }
    // }

    static getDerivedStateFromProps(props, state) {
        const statechanges = {};
        if (props.previous != null) {
            statechanges.previous_cursor = new URL(props.previous).searchParams.get('cursor');
        }
        if (props.next != null) {
            statechanges.next_cursor = new URL(props.next).searchParams.get('cursor');
        }

        return statechanges;
    }

    render() {
        console.log('Pager rendered');
        if (this.props.taskcount == null) {
            return null;
        } else {
            return (
                <div id="paginator" key="paginator">
                    <p key="pagedescription">Showing tasks {this.props.pagefirsttaskposition + 1}-{this.props.pagefirsttaskposition + this.props.pagetaskcount} of {this.props.taskcount}</p>
                    <ul key="prevnext" className="pager">
                        {this.props.previous != null ? <li key="previous" className="previous"><a onClick={() => { this.props.updateCursor(this.state.previous_cursor) }} style={{ cursor: 'pointer' }}>&laquo; Newer</a></li> : null}
                        {this.props.next != null ? <li key="next" className="next"><a onClick={() => { this.props.updateCursor(this.state.next_cursor) }} style={{ cursor: 'pointer' }}>Older &raquo;</a></li> : null}
                    </ul>
                </div>
            )
        }
    }
}

export class TaskPage extends React.Component {
    constructor(props) {
        super(props);

        this.state = {
            taskcount: null,
            results: null,
            scrollToTopAfterUpdate: false,
            dataurl: window.location.href,
            fetchtimeelapsed: null,
        };

        this.newRequest = React.createRef();

        this.setSingleTaskView = this.setSingleTaskView.bind(this);
        this.updateCursor = this.updateCursor.bind(this);
        this.fetchData = this.fetchData.bind(this);
    }

    filterclass(filtername, strurl) {
        // var page_url = new URL(window.location.href);
        const page_url = new URL(strurl);
        const started = page_url.searchParams.get('started');
        if (filtername == null) {
            if (started == null && this.singleTaskViewTaskId(this.state.dataurl) == null) {
                return 'btn-primary'
            } else {
                return 'btn-link'
            }
        } else if (filtername == 'started') {
            if (started == 'true') {
                return 'btn-primary'
            } else {
                return 'btn-link'
            }
        }
    }

    setFilter(filtername) {
        console.log('changed filter to', filtername);
        const new_page_url = new URL(api_url_base);
        new_page_url.search = '';
        if (filtername != null) {
            new_page_url.searchParams.set(filtername, true);
        }

        if (new_page_url != window.location.href) {
            window.history.pushState({}, document.title, new_page_url);
            const statechanges = { 'scrollToTopAfterUpdate': true, dataurl: new_page_url };
            if (filtername == 'started' && this.state.results != null) {
                statechanges['results'] = this.state.results.filter(task => { return task.starttimestamp != null });
                if (statechanges['results'].length == 0) {
                    // prevent flash of "there are no results" for empty ([] non-null) results list
                    statechanges['results'] = null;
                }
            }
            this.setState(statechanges, () => { this.fetchData(true) });
        }
    }

    singleTaskViewTaskId(strurl) {
        const pathext = strurl.toString().replace(
            api_url_base.toString(), '').split('/').filter(el => { return el.length != 0 });

        if (pathext.length == 1 && !isNaN(pathext[0])) {
            return parseInt(pathext[0]);
        } else {
            return null;
        }
    }

    setSingleTaskView(event, task_id, task_url) {
        if (event.ctrlKey || event.metaKey || event.shiftKey) {
            return; // let the browser deal with the click natively
        }
        event.preventDefault();
        const new_page_url = api_url_base + task_id + '/';
        window.history.pushState({}, document.title, new_page_url);

        console.log('Task list changed to single task view for ', new_page_url.toString());

        let newresults = this.state.results.filter(task => { return task.id == task_id });
        if (newresults.length == 0) {
            newresults = null;  // prevent flash of "there are no results" for empty (non-null) results list
        }
        this.setState({
            results: newresults,
            scrollToTopAfterUpdate: true,
            next: null,
            previous: null,
            pagefirsttaskposition: null,
            taskcount: null,
        }, () => { this.fetchData(true) });
    }

    updateCursor(new_cursor) {
        if (new_cursor == new URL(window.location.href).searchParams.get('cursor')) {
            return;
        }
        console.log('Task list cursor changed to ', new_cursor);

        const new_page_url = new URL(window.location.href);
        if (new_cursor != null) {
            new_page_url.searchParams.set('cursor', new_cursor);
        } else {
            new_page_url.searchParams.delete('cursor');
        }
        new_page_url.searchParams.delete('format');

        window.history.pushState({}, document.title, new_page_url);

        this.setState({ scrollToTopAfterUpdate: true }, () => { this.fetchData(true) });
    }

    fetchData(usertriggered) {
        if (document[hidden] || !user_is_active) {
            return;
        }

        this.setState({ dataurl: window.location.href });

        // start by applying a cached version if we have it
        // then send out an HTTP request and update when available
        if (usertriggered) {
            const tasklist_fetchcachematch = (window.location.href in tasklist_fetchcache);
            if (tasklist_fetchcachematch) {
                console.log('using tasklist_fetchcache before GET response', window.location.href);
                this.setState(tasklist_fetchcache[window.location.href]);
            } else {
                console.log('no tasklist_fetchcache for', window.location.href);
            }
        }

        if (tasklist_api_request_active && !usertriggered) {
            console.log('prevent overlapping GET requests');
            return;
        }

        tasklist_api_request_active = true;
        const get_url = window.location.href;
        console.log('Fetching task list from', get_url);
        fetch(get_url,
            {
                credentials: "same-origin",
                method: "GET",
                headers: {
                    "X-CSRFToken": getCookie("csrftoken"),
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
                redirect: "manual"
            })
            .then((response) => {
                tasklist_api_error = '';
                tasklist_api_request_active = false;
                // etag = response.headers.get('ETag');
                if (response.type === "opaqueredirect") {
                    // redirect to login page
                    window.location.href = response.url;
                    console.log('Fetch got a redirection to ', response.url);
                } else {
                    if (response.status != 200) {
                        console.log("Fetch received HTTP status ", response.status);
                    }
                    if (response.status == 404) {
                        window.history.pushState({}, document.title, api_url_base);
                        this.setState({ scrollToTopAfterUpdate: true }, () => { this.fetchData(true) });
                    }
                    if (response.status == 200) {
                        return response.json();
                    }
                }
                return null;
            }).catch(error => {
                tasklist_api_request_active = false;
                console.log('Get task list HTTP request failed', error);
                tasklist_api_error = 'Connection error';
            }).then(data => {
                let statechanges = null;
                if (data != null && data.hasOwnProperty('results')) {
                    if (data.results.length == 0 && new URL(window.location.href).searchParams.get('cursor') != null) {
                        // page is empty. redirect to main page
                        this.updateCursor(null);
                    } else {
                        statechanges = data;
                    }
                } else if (data != null && data.hasOwnProperty('id')) {
                    // single task view doesn't put task data inside 'results' list,
                    // so we create a single-item results list
                    statechanges = {
                        results: [data],
                        next: null,
                        previous: null,
                        pagefirsttaskposition: null,
                        taskcount: null,
                    };
                }
                if (statechanges != null) {
                    statechanges['tasklist_last_fetch_time'] = new Date();
                    tasklist_fetchcache[window.location.href] = statechanges;
                    if (get_url == window.location.href) {
                        console.log('Applying results from', get_url);
                        if (usertriggered) {
                            statechanges['scrollToTopAfterUpdate'] = true
                        }
                        this.setState(statechanges);
                    } else {
                        console.log('Not applying results from', get_url, 'location.href', window.location.href);
                        return;
                    }
                }
            });
    }

    componentDidUpdate() {
        if (this.state.scrollToTopAfterUpdate) {
            this.setState({ scrollToTopAfterUpdate: false });
            window.scrollTo(0, 0);
            window.dispatchEvent(new Event('resize'));
        }
    }

    componentDidMount() {
        this.fetchinterval = setInterval(() => this.fetchData(false), 2000);
        this.fetchData(true);
    }

    componentWillUnmount() {
        clearInterval(this.fetchinterval);
    }

    render() {
        // console.log('TaskPage rendered');
        const singletaskmode = this.singleTaskViewTaskId(this.state.dataurl) != null;
        let pagehtml = [];
        if (!singletaskmode) {
            pagehtml.push(<div key="header" className="page-header"><h1>Task Queue</h1></div>);
        } else {
            pagehtml.push(<div key="header" className="page-header"><h1>Task {this.singleTaskViewTaskId(this.state.dataurl)}</h1></div>);
        }

        if (!singletaskmode || (this.state.results != null && this.state.results.length > 0 && this.state.results[0].user_id == user_id)) {
            pagehtml.push(
                <ul key="filters" id="taskfilters">
                    <li key="all"><a onClick={() => this.setFilter(null)} className={'btn ' + this.filterclass(null, this.state.dataurl)}>All tasks</a></li>
                    <li key="started"><a onClick={() => this.setFilter('started')} className={'btn ' + this.filterclass('started', this.state.dataurl)}>Running/Finished</a></li>
                </ul>);
        }

        if (this.state.tasklist_last_fetch_time != null) {
            pagehtml.push(<p key="tasklistfetchstatus" id='tasklistfetchstatus'>Last updated: {this.state.tasklist_last_fetch_time.toLocaleString()} <span className="errors">{tasklist_api_error}</span></p>);
        }

        if (!singletaskmode) {
            const allow_stack_rock = new URL(this.state.dataurl).searchParams.get('allow_stack_rock') == 'true';

            pagehtml.push(<NewRequest key="newrequest" fetchData={this.fetchData} allow_stack_rock={allow_stack_rock} />);
        }

        let tasklist;
        if (this.state.results == null) {
            tasklist = <p key="message">Loading tasks...</p>;
        } else if (this.state.results.length == 0) {
            tasklist = <p key="message">There are no tasks.</p>;
        } else {
            const pagetaskcount = (this.state.results != null) ? this.state.results.length : null;
            tasklist = [
                <ul key="ultasklist" className="tasks">
                    {this.state.results.map((task) => (<Task key={task.id} taskdata={task} fetchData={this.fetchData} setSingleTaskView={this.setSingleTaskView} hidePlot={pagetaskcount > 10} />))}
                </ul>,
                <Pager key='pager' previous={this.state.previous} next={this.state.next} pagefirsttaskposition={this.state.pagefirsttaskposition} pagetaskcount={pagetaskcount} taskcount={this.state.taskcount} updateCursor={this.updateCursor} />
            ];
        }

        pagehtml.push(<div key="tasklist" id="tasklist" className={singletaskmode ? 'singletaskdetail' : null}>{tasklist}</div>);

        return pagehtml;
    }
}


const container = document.getElementById('taskpage');
const root = ReactDOM.createRoot(container);
root.render(<TaskPage />);
