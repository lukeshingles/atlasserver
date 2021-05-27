'use strict';

// const apiUrl = 'http://127.0.0.1:8000/';

class Task extends React.Component {
  constructor(props) {
    super(props);
    this.state = props.taskdata
    this.deleteTask = this.deleteTask.bind(this);
    this.state.timeelapsed = -1;
  }

  deleteTask() {
    $.ajax({url: this.state.url, method: 'delete', success: function (result) {updatePageTasks();}});
  }

  static getDerivedStateFromProps(props, state) {

    if (props.taskdata.starttimestamp != null && props.taskdata.finishtimestamp == null) {
      var starttime = new Date( props.taskdata.starttimestamp ).getTime();
      var now = new Date().getTime();
      var timeelapsed = (now - starttime) / 1000.;
      props.taskdata.timeelapsed = timeelapsed.toFixed(1);
    }

    if (JSON.stringify(props.taskdata) != JSON.stringify(state))
    {
      // console.log('Task ' + this.state.id + ' changed');
      return props.taskdata;
    }
    return null;
  }


  componentDidMount() {
    if (this.state.starttimestamp != null && this.state.finishtimestamp == null) {
      console.log(this.state.starttimestamp);
      this.interval = setInterval(() => this.updateTimeElapsed(), 300);
    }
  }

  componentWillUnmount() {
    clearInterval(this.interval);
  }

  updateTimeElapsed() {
    if (this.state.starttimestamp != null && this.state.finishtimestamp == null) {
      var starttime = new Date( this.state.starttimestamp ).getTime();
      var now = new Date().getTime();
      var timeelapsed = (now - starttime) / 1000.;
      this.setState({'timeelapsed': timeelapsed.toFixed(1)});
    } else {
      clearInterval(this.interval);
    }
  }

  shouldComponentUpdate(nextProps, nextState) {
    if (this.state.starttimestamp != null && this.state.finishtimestamp == null) {
      return true;
    }
    if (JSON.stringify(nextState) == JSON.stringify(this.state)) {
      return false;
    } else {
      return true;
    }
  }

  render() {
    var task = this.state;
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
    var taskbox = [
      <div key="rightside" className="rightside">
          <button className="btn btn-sm btn-danger" onClick={this.deleteTask}>{buttontext}</button>
          <img src={task.previewimage_url} style={{display: 'block', marginTop: '1em', marginLeft: '1em'}} />
      </div>
    ];

    taskbox.push(<div key="tasknum"><a key="tasklink" href={task.url + '?usereact'}>Task {task.id}</a></div>);

    if (task.parent_task_url) {
      taskbox.push(<p key="imgrequest">Image request for <a key="parent_task_link" href={task.parent_task_url + '?usereact'}>Task {task.parent_task_id}</a></p>);
    } else if (task.parent_task) {
      taskbox.push(<p key="imgrequest">Image request for Task {task.parent_task_id} (deleted)</p>);
    }
    if (task.parent_task_id) {
      var imagetype = task.use_reduced ? 'reduced' : 'difference';
      taskbox.push(<p key="imgrequestnote">Up to the first 500 {imagetype} images will be retrieved. The image request and download link may expire after one week.</p>);
    }

    if (task.user_id != userid) {
      taskbox.push(<div key="user">User: {task.username}</div>);
    }

    if (task.comment != '') {
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

    taskbox.push(<div key="queuetime">Queued at {task.timestamp}</div>);
    if (task.finishtimestamp != null) {
      taskbox.push(<div key="status">Finished at {task.finishtimestamp}</div>);
      if (task.error_msg != null) {
        taskbox.push(<p style={{color: 'black', fontWeight: 'bold'}}>Error: {task.error_msg}</p>);
      } else {
        if (task.request_type == 'FP') {
          taskbox.push(<a key="datalink" className="results btn btn-info getdata" href={task.result_url} target="_blank">Data</a>);
          taskbox.push(<a key="pdflink" className="results btn btn-info getpdf" href={task.pdfplot_url} target="_blank">PDF</a>);
        }
      }
    } else if (task.starttimestamp != null) {
      taskbox.push(<div key="status" style={{color: 'red', fontStyle: 'italic'}}>Running (started {this.state.timeelapsed} seconds ago)</div>);
    } else {
      taskbox.push(<div key="status">Waiting ({task.queuepos} tasks ahead of this one)</div>);
    }

    if (task.finishtimestamp != null && task.error_msg == null && task.request_type == 'FP') {
      taskbox.push(<div key='plot' id={'plotforcedflux-task-' + task.id} className="plot" style={{width: '100%', height: '300px'}}></div>);
    }

    return (
      <li key={"task-" + task.id} className={"task " + statusclass} id={"task-" + task.id}>
      {taskbox}
      </li>
    );
  }
}

class Pager extends React.Component {
  constructor(props) {
    super(props);

    this.state = {}
    this.state.previous = this.props.previous;
    this.state.next = this.props.next;
    this.state.pagetaskcount = this.props.pagetaskcount;
    this.state.taskcount = this.props.taskcount;
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
    if (props.previous != state.previous)
      statechanges.previous = props.previous;

    if (props.next != state.next)
      statechanges.next = props.next;

    if (props.pagetaskcount != state.pagetaskcount)
      statechanges.pagetaskcount = props.pagetaskcount;

    if (props.previous != state.previous)
      statechanges.taskcount = props.taskcount;

    if (Object.keys(statechanges).length > 0)
    {
      console.log('Pager changed');
      return statechanges;
    }
    return null;
  }

  shouldComponentUpdate(nextProps, nextState) {
    if (JSON.stringify(this.props) === JSON.stringify(nextProps)) {
      return false;
    } else {
      return true;
    }
  }

  render() {
    console.log('Pager rendered');
    if (this.state.taskcount == null) {
      return null;
    } else {
      return (
        <div id="paginator">
            <p id="taskcount">Showing {this.state.pagetaskcount} of {this.state.taskcount} tasks</p>
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
      'status': 'Loading...'
    };
  }

  fetchData() {
    console.log('Fetching data...');
    fetch(this.props.apiURL,
    {
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json'
      }
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.hasOwnProperty('results')) {
          this.setState(data);
        } else {
          // single task view doesn't put task data inside 'results' list,
          // so we create a single-item results list
          this.setState({results: [data]});
        }
      });
  }

  componentDidMount() {
    this.fetchData();
    this.interval = setInterval(() => this.fetchData(), 2000);
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
        <>
          <ul className="tasks">
          {this.state.results.map((task) => (<Task key={task.id} taskdata={task} />))}
          </ul>
          <Pager key='pager' previous={this.state.previous} next={this.state.next} pagetaskcount={pagetaskcount} taskcount={this.state.taskcount} />
        </>
      );
    }
  }
}

ReactDOM.render(React.createElement(TaskList, {apiURL: apiURL}), document.getElementById('tasklist'));
