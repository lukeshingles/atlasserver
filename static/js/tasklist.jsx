'use strict';

var jslcdataglobal = new Object();
var jslabelsglobal = new Object();
var jslimitsglobal = new Object();

var api_request_active = false;

class TaskPlot extends React.Component {
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
    } else if (task.parent_task) {
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


function getCursor(str_json_url) {
  if (str_json_url == null) {
    return null;
  }
  var json_url = new URL(str_json_url);
  return json_url.searchParams.get('cursor');
}


class Pager extends React.Component {
  constructor(props) {
    super(props);

    this.state = {}
    this.state.previous_cursor = getCursor(this.props.previous);
    this.state.next_cursor = getCursor(this.props.next);
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
    statechanges.previous_cursor = getCursor(props.previous);
    statechanges.next_cursor = getCursor(props.next);

    return statechanges;
  }

  shouldComponentUpdate(nextProps, nextState) {
    if (JSON.stringify(this.props) == JSON.stringify(nextProps)) {
      return false;
    } else {
      return true;
    }
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

class TaskList extends React.Component {
  constructor(props) {
    super(props);

    this.state = {
      'taskcount': null,
      'results': null,
      'status': 'Loading...',
      'api_url': props.api_url,
    };

    this.setSingleTaskView = this.setSingleTaskView.bind(this);
    this.updateCursor = this.updateCursor.bind(this);
    this.fetchData = this.fetchData.bind(this);
  }

  setSingleTaskView(task_id, task_url) {
    console.log('Task list changed to single task view for ', task_url);

    this.setState({'api_url': task_url}, () => {this.fetchData(true)});

    var new_page_url = new URL(task_url);
    new_page_url.searchParams.delete('format');
    window.history.pushState({}, document.title, new_page_url);

    $('.newrequest').hide();
    $('#tasklist').addClass('singletaskdetail');

    var div_plot_id = 'plotforcedflux-task-' + task_id;
    if ($('#' + div_plot_id).length != 0) {
      var w = $('#' + div_plot_id).innerWidth();
      Plotly.relayout(div_plot_id, {
        width: w, //window.innerWidth,
        height: lcplotheight //window.innerHeight
      });
    };
  }

  updateCursor(new_cursor) {
    if (new_cursor == getCursor(this.state.api_url)) {
      return;
    }
    console.log('Task list cursor changed to ', new_cursor);

    var new_api_url = new URL(this.state.api_url);
    if (new_cursor != null) {
      new_api_url.searchParams.set('cursor', new_cursor);
    } else {
      new_api_url.searchParams.delete('cursor');
    }
    this.setState({'api_url': new_api_url.toString()}, () => {this.fetchData(true)});

    var new_page_url = new URL(window.location.href);
    if (new_cursor != null) {
      new_page_url.searchParams.set('cursor', new_cursor);
    } else {
      new_page_url.searchParams.delete('cursor');
    }
    new_page_url.searchParams.delete('format');

    window.history.pushState({}, document.title, new_page_url);
  }

  fetchData(scrollUpAfter) {
    if (document[hidden] || !user_is_active) {
      return;
    }

    if (api_request_active) {
      console.log('prevent overlapping GET requests');
      return;
    }

    api_request_active = true;
    console.log('Fetching task list from ', this.state.api_url);
    fetch(this.state.api_url,
    {
      ifModified: true,
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      }
    })
    .then((response) => {
      api_request_active = false;
      return response.json();
    }).catch(error => {
      console.log('HTTP request failed', error);
    }).then((data) => {
      if (data.hasOwnProperty('results')) {
        this.setState(data);
        if (this.state.results.length == 0 && getCursor(this.state.api_url) != null) {
          this.updateCursor(null);
        }
      } else {
        // single task view doesn't put task data inside 'results' list,
        // so we create a single-item results list
        this.setState({results: [data]});
      }
      if (scrollUpAfter) {
        window.scrollTo(0, 0);
      }
    });
  }

  // shouldComponentUpdate(nextProps, nextState) {
  //   if (nextState.api_url != this.state.api_url) {
  //     this.fetchData(true);
  //     return true;
  //   } else if (JSON.stringify(nextState) != JSON.stringify(this.state)) {
  //     return true;
  //   } else {
  //     return false;
  //   }
  // }

  componentDidMount() {
    this.fetchData(false);
    this.interval = setInterval(() => this.fetchData(), 3000);
  }

  componentWillUnmount() {
    clearInterval(this.interval);
  }

  render() {
    if (this.state.results == null) {
      return <p>Loading tasks...</p>;
    } else if (this.state.results.length == 0) {
      return <p>There are no tasks.</p>;
    } else {
      var pagetaskcount = (this.state.results != null) ? this.state.results.length : null;
      return (
        <div>
          <ul key="tasklist" className="tasks">
          {this.state.results.map((task) => (<Task key={task.id} taskdata={task} fetchData={this.fetchData} setSingleTaskView={this.setSingleTaskView} />))}
          </ul>
          <Pager key='pager' previous={this.state.previous} next={this.state.next} pagefirsttaskposition={this.state.pagefirsttaskposition} pagetaskcount={pagetaskcount} taskcount={this.state.taskcount} updateCursor={this.updateCursor} />
        </div>
      );
    }
  }
}

ReactDOM.render(React.createElement(TaskList, {api_url: api_url}), document.getElementById('tasklist'));
