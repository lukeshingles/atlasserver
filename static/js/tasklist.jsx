'use strict';

var jslcdataglobal = new Object();
var jslabelsglobal = new Object();
var jslimitsglobal = new Object();

var api_request_active = false;

class TaskPlot extends React.PureComponent {
  constructor(props) {
    super(props);
  }

  componentDidMount() {
    console.log('activating plot', this.props.taskid)
    var plot_url = new URL(this.props.taskurl);
    plot_url.pathname += 'resultplotdata.js';
    plot_url.search = '';
    $.ajax({url: plot_url, cache: true, dataType: 'script'});
  }

  componentWillUnmount() {
    console.log('Unmounting plot for task ', this.props.taskid);
    delete jslimitsglobal['#plotforcedflux-task-' + this.props.taskid]
    delete jslcdataglobal['#plotforcedflux-task-' + this.props.taskid]
    delete jslabelsglobal['#plotforcedflux-task-' + this.props.taskid]
  }

  render() {
    return (
      <div key='plot' id={'plotforcedflux-task-' + this.props.taskid} className="plot" style={{width: '100%', height: '300px'}}></div>
    );
  }
}

class Task extends React.Component {
  constructor(props) {
    super(props);
    this.requestImages = this.requestImages.bind(this);
    this.deleteTask = this.deleteTask.bind(this);
    this.state = {}
    this.state.updateTimeElapsed = this.updateTimeElapsed.bind(this);
    this.state.interval = null;
    this.state.timeelapsed = -1;
  }

  deleteTask() {
    $.ajax({url: this.props.taskdata.url, method: 'delete', success: (result) => {this.props.fetchData()}});
  }

  requestImages() {
    var request_image_url = new URL(this.props.taskdata.url);
    request_image_url.pathname += 'requestimages';
    request_image_url.search = '';
    $.ajax({url: request_image_url, method: 'get', success: (result) => {this.props.fetchData()}});
  }

  static getDerivedStateFromProps(props, state) {
    var statechanges = {};
    if (props.taskdata.starttimestamp != null && props.taskdata.finishtimestamp == null) {
      if (state.interval == null) {
        var starttime = new Date(props.taskdata.starttimestamp).getTime();
        var timeelapsed = (new Date().getTime() - starttime) / 1000.;
        return {'interval': setInterval(state.updateTimeElapsed, 1000), 'timeelapsed': timeelapsed.toFixed(0)};
      }
    } else if (state.interval != null) {
      return {'interval': null};
    }

    return null;
  }


  componentDidMount() {
    this.updateTimeElapsed();
    if (newtaskids.includes(this.props.taskdata.id)) {
      var li_id = '#task-' + this.props.taskdata.id
      $(li_id).hide();
      $(li_id).show(700);
      console.log('new task', this.props.taskdata.id);
      newtaskids = newtaskids.filter(item => item !== this.props.taskdata.id)
    }

    // this.interval = setInterval(() => {this.updateTimeElapsed()}, 1000);
  }

  componentWillUnmount() {
    clearInterval(this.state.interval);
    // this.state.interval = null;
  }

  updateTimeElapsed() {
    if (this.props.taskdata.starttimestamp != null && this.props.taskdata.finishtimestamp == null) {
      var starttime = new Date(this.props.taskdata.starttimestamp).getTime();
      var timeelapsed = (new Date().getTime() - starttime) / 1000.;
      this.setState({'timeelapsed': timeelapsed.toFixed(0)});
    } else if (this.state.interval != null) {
      clearInterval(this.state.interval);
      this.setState({'interval': null});
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
    var task = this.props.taskdata;
    if (task == null) {
      return;
    }
    var statusclass = 'none';
    var buttontext = 'none';
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
    var delbutton = null;
    if (task.user_id == user_id) {
       delbutton = <button className="btn btn-sm btn-danger" onClick={this.deleteTask}>{buttontext}</button>;
    }
    var taskbox = [
      <div key="rightside" className="rightside">
          {delbutton}
          <img src={task.previewimage_url} style={{display: 'block', marginTop: '1em', marginLeft: '1em'}} />
      </div>
    ];

    taskbox.push(<div key="tasknum"><a key="tasklink" onClick={() => {this.props.setSingleTaskView(task.id, task.url)}}>Task {task.id}</a></div>);

    if (task.parent_task_url) {
      taskbox.push(<p key="imgrequest">Image request for <a key="parent_task_link" onClick={() => {this.props.setSingleTaskView(task.parent_task_id, task.parent_task_url)}}>Task {task.parent_task_id}</a></p>);
    } else if (task.parent_task_id) {
      taskbox.push(<p key="imgrequest">Image request for Task {task.parent_task_id} (deleted)</p>);
    }
    if (task.parent_task_id) {
      var imagetype = task.use_reduced ? 'reduced' : 'difference';
      taskbox.push(<p key="imgrequestnote">Up to the first 500 {imagetype} images will be retrieved. The image request and download link may expire after one week.</p>);
    }

    if (task.user_id != user_id) {
      taskbox.push(<div key="user">User: {task.username}</div>);
    }

    if (task.comment != null && task.comment != '') {
      taskbox.push(<div key="comment">Comment: <b>{task.comment}</b></div>);
    }

    if (task.mpc_name != null) {
      taskbox.push(<div key="target">MPC Object: {task.mpc_name}</div>);
    } else {
      taskbox.push(<div key="target">RA Dec: {task.ra} {task.dec}</div>);
    }
    taskbox.push(<div key="imgtype">Images: {task.use_reduced ? 'Reduced' : 'Difference'}</div>);

    if (task.mjd_min != null || task.mjd_max != null) {
        var mjdmin = task.mjd_min != null ? task.mjd_min : "0";
        var mjdmax = task.mjd_max != null ? task.mjd_max : "∞";
        taskbox.push(<div key="mjdrange">MJD range: [{mjdmin}, {mjdmax}]</div>);
    }

    taskbox.push(<div key="queuetime">Queued at {new Date(task.timestamp).toLocaleString()}</div>);
    if (task.finishtimestamp != null) {
      taskbox.push(<div key="status">Finished at {new Date(task.finishtimestamp).toLocaleString()}</div>);
      if (task.error_msg != null) {
        taskbox.push(<p key="error_msg" style={{color: 'black', fontWeight: 'bold', marginTop: '1em'}}>Error: {task.error_msg}</p>);
      } else {
        if (task.request_type == 'FP') {
          taskbox.push(<a key="datalink" className="results btn btn-info getdata" href={task.result_url} target="_blank">Data</a>);
          taskbox.push(<a key="pdflink" className="results btn btn-info getpdf" href={task.pdfplot_url} target="_blank">PDF</a>);
        }

        if (task.request_type == 'IMGZIP') {
          if (task.result_imagezip_url != null) {
            taskbox.push(<a key="imgdownload" className="results btn btn-info" href={task.result_imagezip_url}>Download images (ZIP)</a>);
          }
        } else if (task.imagerequest_task_id != null) {
          if (task.imagerequest_finished) {
            taskbox.push(<a key="imgrequest" className="btn btn-primary" onClick={() => {this.props.setSingleTaskView(task.imagerequest_task_id, task.imagerequest_url)}}>Images ready</a>);
          } else {
            taskbox.push(<a key="imgrequest" className="btn btn-warning" onClick={() => {this.props.setSingleTaskView(task.imagerequest_task_id, task.imagerequest_url)}}>Images requested</a>);
          }
        } else if (user_id == task.user_id) {
            taskbox.push(<button key="imgrequest" className="btn btn-info" onClick={this.requestImages} title="Download FITS and JPEG images for up to the first 500 observations.">Request {task.use_reduced ? 'reduced' : 'diff'} images</button>);
        }
      }
    } else if (task.starttimestamp != null) {
      taskbox.push(<div key="status" style={{color: 'red', fontStyle: 'italic', marginTop: '1em'}}>Running (started {this.state.timeelapsed} seconds ago)</div>);
    } else {
      taskbox.push(<div key="status" style={{fontStyle: 'italic', marginTop: '1em'}}>Waiting ({task.queuepos} tasks ahead of this one)</div>);
    }

    if (task.finishtimestamp != null && task.error_msg == null && task.request_type == 'FP') {
      taskbox.push(<TaskPlot key='plot' taskid={task.id} taskurl={task.url} />);
    }

    return (
      <li key={"task-" + task.id} className={"task " + statusclass} id={"task-" + task.id}>
      {taskbox}
      </li>
    );
  }
}


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
    var statechanges = {};
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
              {this.props.previous != null ? <li key="previous" className="previous"><a onClick={() => {this.props.updateCursor(this.state.previous_cursor)}} style={{cursor: 'pointer'}}>&laquo; Newer</a></li> : null}
              {this.props.next != null ? <li key="next" className="next"><a onClick={() => {this.props.updateCursor(this.state.next_cursor)}} style={{cursor: 'pointer'}}>Older &raquo;</a></li> : null}
            </ul>
        </div>
      )
    }
  }
}

class TaskPage extends React.Component {
  constructor(props) {
    super(props);

    this.state = {
      'taskcount': null,
      'results': null,
      'status': 'Loading...',
    };

    this.state.scrollToTopAfterUpdate = false;

    this.newRequest = React.createRef();

    this.singleTaskViewTaskId = this.singleTaskViewTaskId.bind(this);
    this.setFilter = this.setFilter.bind(this);
    this.setSingleTaskView = this.setSingleTaskView.bind(this);
    this.updateCursor = this.updateCursor.bind(this);
    this.fetchData = this.fetchData.bind(this);
  }

  filterclass(filtername) {
    var new_page_url = new URL(window.location.href);
    if (filtername == null) {
      if (new_page_url.searchParams.get('started') == null && this.singleTaskViewTaskId() == null) {
        return 'btn-primary'
      } else {
        return 'btn-link'
      }
    } else if (filtername == 'started') {
      if (new_page_url.searchParams.get('started') == 'true') {
        return 'btn-primary'
      } else {
        return 'btn-link'
      }
    }
  }

  setFilter(filtername) {
    var new_page_url = new URL(api_url_base);
    new_page_url.search = '';
    if (filtername != null) {
      new_page_url.searchParams.set(filtername, true);
    }

    if (new_page_url != window.location.href) {
      window.history.pushState({}, document.title, new_page_url);
      this.setState({'scrollToTopAfterUpdate': true}, () => {this.fetchData()});
    }
  }

  singleTaskViewTaskId() {
    var pathext = window.location.href.replace(
      api_url_base, '').split('/').filter(el => {return el.length != 0});

    if (pathext.length == 1 && !isNaN(pathext[0])) {
      return parseInt(pathext[0]);
    } else {
      return null;
    }
  }

  setSingleTaskView(task_id, task_url) {
    var new_page_url = api_url_base + task_id + '/';
    window.history.pushState({}, document.title, new_page_url);

    console.log('Task list changed to single task view for ', new_page_url.toString());

    this.setState({
      results: this.state.results.filter(task => {return task.id == task_id}),
      scrollToTopAfterUpdate: true,
      next: null,
      previous: null,
      pagefirsttaskposition: null,
      taskcount: null,
    }, () => {this.fetchData()});
  }

  updateCursor(new_cursor) {
    if (new_cursor == new URL(window.location.href).searchParams.get('cursor')) {
      return;
    }
    console.log('Task list cursor changed to ', new_cursor);

    var new_page_url = new URL(window.location.href);
    if (new_cursor != null) {
      new_page_url.searchParams.set('cursor', new_cursor);
    } else {
      new_page_url.searchParams.delete('cursor');
    }
    new_page_url.searchParams.delete('format');

    window.history.pushState({}, document.title, new_page_url);

    this.setState({scrollToTopAfterUpdate: true}, () => {this.fetchData()});
  }

  fetchData() {
    if (document[hidden] || !user_is_active) {
      return;
    }

    if (api_request_active) {
      console.log('prevent overlapping GET requests');
      return;
    }

    api_request_active = true;
    console.log('Fetching task list from ', window.location.href);
    fetch(window.location.href,
    {
      ifModified: true,
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      }
    })
    .then((response) => {
      api_request_active = false;
      if (response.status != 200) {
        console.log("Fetch recieved HTTP status ", response.status);
      }
      if (response.status == 404) {
        window.history.pushState({}, document.title, api_url_base);
        this.setState({scrollToTopAfterUpdate: true}, () => {this.fetchData()});
      }
      return response.json();
    }).catch(error => {
      console.log('HTTP request failed', error);
    }).then((data) => {
      if (data == null) {
        return;
      } else if (data.hasOwnProperty('results')) {
        this.setState(data);
        if (data.results.length == 0 && getCursor(window.location.href) != null) {
          // page is empty. redirect to main page
          this.updateCursor(null);
        }
      } else {
        // single task view doesn't put task data inside 'results' list,
        // so we create a single-item results list
        this.setState({
          results: [data],
          next: null,
          previous: null,
          pagefirsttaskposition: null,
          taskcount: null,
        });
      }
    });
  }

  componentDidUpdate() {
    if (this.state.scrollToTopAfterUpdate) {
      window.scrollTo(0, 0);
      this.setState({scrollToTopAfterUpdate: false});
      window.dispatchEvent(new Event('resize'));
    }
  }

  componentDidMount() {
    this.interval = setInterval(() => this.fetchData(), 3000);
    this.fetchData();

    // Declare a fragment:
    // var fragment = document.createDocumentFragment();
    // Append desired element to the fragment:
    // fragment.appendChild(document.getElementById('newrequestsource'));


    // Append fragment to desired element:
    // document.getElementById('this.newRequest').appendChild(fragment);
    // this.newRequest.current.appendChild(document.getElementById('newrequestsource'));

    this.newRequest.current.appendChild(document.getElementById('newrequestsource'));
  }

  componentWillUnmount() {
    clearInterval(this.interval);
  }

  render() {
    var singletaskmode = this.singleTaskViewTaskId() != null;
    var pagehtml = [];
    if (!singletaskmode) {
      pagehtml.push(<div key="header" className="page-header"><h1>Task Queue</h1></div>);
    } else {
      pagehtml.push(<div key="header" className="page-header"><h1>Task {this.singleTaskViewTaskId()}</h1></div>);
    }

    pagehtml.push(
      <ul key="filters" id="taskfilters">
        <li key="all"><a onClick={() => this.setFilter(null)} className={'btn ' + this.filterclass(null)}>All tasks</a></li>
        <li key="started"><a onClick={() => this.setFilter('started')} className={'btn ' + this.filterclass('started')}>Running/Finished</a></li>
      </ul>);

    var newrequeststyle = singletaskmode ? {display: 'none'} : null;
    pagehtml.push(<div key="newrequest" id="newrequestcontainer" ref={this.newRequest} style={newrequeststyle}></div>);

    var tasklist;
    if (this.state.results == null) {
      tasklist = <p key="message">Loading tasks...</p>;
    } else if (this.state.results.length == 0) {
      tasklist = <p key="message">There are no tasks.</p>;
    } else {
      var pagetaskcount = (this.state.results != null) ? this.state.results.length : null;
      tasklist = [
        <ul key="ultasklist" className="tasks">
          {this.state.results.map((task) => (<Task key={task.id} taskdata={task} fetchData={this.fetchData} setSingleTaskView={this.setSingleTaskView} />))}
        </ul>,
        <Pager key='pager' previous={this.state.previous} next={this.state.next} pagefirsttaskposition={this.state.pagefirsttaskposition} pagetaskcount={pagetaskcount} taskcount={this.state.taskcount} updateCursor={this.updateCursor} />
      ];
    }

    pagehtml.push(<div key="tasklist" id="tasklist" className={singletaskmode ? 'singletaskdetail' : null}>{tasklist}</div>);

    return pagehtml;
  }
}

ReactDOM.render(React.createElement(TaskPage, {}), document.getElementById('taskpage'));
