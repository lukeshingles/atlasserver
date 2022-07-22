'use strict';

var jslcdataglobal = new Object();
var jslabelsglobal = new Object();
var jslimitsglobal = new Object();

var tasklist_api_request_active = false;
var tasklist_fetchcache = [];
var tasklist_api_error = '';
var submission_in_progress = false;

function getDefaultMjdMin() {
  return (mjdFromDate(new Date()) - 30.).toFixed(5);
}

class NewRequest extends React.Component {
  get_defaultstate() {
    return {
      showradechelp: false,
      radeclist: localStorage.getItem('radeclist') != null ? localStorage.getItem('radeclist') : '',
      mjd_min: localStorage.getItem('mjd_min') != null? localStorage.getItem('mjd_min') : getDefaultMjdMin(),
      mjd_max: localStorage.getItem('mjd_max') != null ? localStorage.getItem('mjd_max') : '',
      comment: localStorage.getItem('comment') != null ? localStorage.getItem('comment') : '',
      use_reduced: localStorage.getItem('use_reduced') == 'true',
      send_email: localStorage.getItem('send_email') != 'false',
      enable_propermotion: localStorage.getItem('enable_propermotion') == 'true',
      radec_epoch_year: localStorage.getItem('radec_epoch_year') != null ? localStorage.getItem('radec_epoch_year') : '',
      propermotion_ra: localStorage.getItem('propermotion_ra') != null ? localStorage.getItem('propermotion_ra') : 0.,
      propermotion_dec: localStorage.getItem('propermotion_dec') != null ? localStorage.getItem('propermotion_dec') : 0.,
      errors: [],
      httperror: '',
      submission_in_progress: false,  // duplicated to trigger a render
    };
  }

  constructor(props) {
    super(props);

    this.state = this.get_defaultstate();

    this.handlechange_mjd_min = this.handlechange_mjd_min.bind(this);
    this.update_mjd_min = this.update_mjd_min.bind(this);
    this.handlechange_mjd_max = this.handlechange_mjd_max.bind(this);
    this.update_mjd_max = this.update_mjd_max.bind(this);
    this.submit = this.submit.bind(this);
  }

  componentDidMount() {
    this.update_mjd_min(this.state.mjd_min);
    this.update_mjd_max(this.state.mjd_max);
  }

  update_mjd_min(strmjdmin) {
    var isostrmin = '';
    if (strmjdmin == '') {
      isostrmin = '(leave blank to fetch earliest)'
    } else {
      try {
        var mjdmin = parseFloat(strmjdmin);
        var isostr_withmilliseconds = dateFromMJD(mjdmin).toISOString();
        isostrmin = (
            isostr_withmilliseconds.includes('.') ?
            isostr_withmilliseconds.split('.')[0] + 'Z' : isostr_withmilliseconds);
      }
      catch(err) {
        isostrmin = 'error'
        console.log('error', err, err.message);
      }
    }
    this.setState({'mjd_min': strmjdmin, 'mjd_min_isoformat': isostrmin});
  }

  handlechange_mjd_min(event) {
    this.update_mjd_min(event.target.value);
    localStorage.setItem('mjd_min', event.target.value);
  }

  update_mjd_max(strmjdmax) {
    var isostrmax = '';
    if (strmjdmax == '') {
      isostrmax = '(leave blank to fetch latest)'
    } else {
      try {
        var mjdmax = parseFloat(strmjdmax);
        console.log("invalid?", strmjdmax, mjdmax);
        var isostr_withmilliseconds = dateFromMJD(mjdmax).toISOString();
        isostrmax = (
            isostr_withmilliseconds.includes('.') ?
            isostr_withmilliseconds.split('.')[0] + 'Z' : isostr_withmilliseconds);
      }
      catch(err) {
        isostrmax = 'error'
        console.log('error', err, err.message);
      }
    }
    this.setState({'mjd_max': strmjdmax, 'mjd_max_isoformat': isostrmax});
    localStorage.setItem('mjd_max', strmjdmax);
  }

  handlechange_mjd_max(event) {
    this.update_mjd_max(event.target.value);
  }

  async submit() {
    var datadict = {
      radeclist: this.state.radeclist,
      mjd_min: this.state.mjd_min == '' ? null : this.state.mjd_min,
      mjd_max: this.state.mjd_max == '' ? null : this.state.mjd_max,
      use_reduced: this.state.use_reduced,
      use_email: this.state.use_email,
    };

    if (this.state.enable_propermotion) {
      datadict['radec_epoch_year'] = this.state.radec_epoch_year;
      datadict['propermotion_ra'] = this.state.propermotion_ra;
      datadict['propermotion_dec'] = this.state.propermotion_dec;
    }
    console.log(datadict)

    fetch(api_url_base,
    {
      credentials: "same-origin",
      method: "POST",
      body: JSON.stringify(datadict),
      headers: {
        "X-CSRFToken": getCookie("csrftoken"),
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      },
    })
    .catch(error => {
      submission_in_progress = false;
      console.log('New task HTTP request failed', error);
      this.setState({'httperror': 'HTTP request failed.', 'submission_in_progress': false});
    })
    .then((response) => {
      submission_in_progress = false;
      this.setState({'httperror': '', 'submission_in_progress': false});
      console.log('New task: HTTP response ', response.status);

      if (response.status == 201) {
        console.log("New task: successful creation", response.status);
        localStorage.removeItem('radeclist');
        localStorage.removeItem('enable_propermotion');
        localStorage.removeItem('radec_epoch_year');
        localStorage.removeItem('propermotion_ra');
        localStorage.removeItem('propermotion_dec');
        localStorage.removeItem('mjd_min');
        localStorage.removeItem('mjd_max');
        localStorage.removeItem('comment');

        this.setState(this.get_defaultstate());
        response.json().then(data => {
          // console.log('Creation data', data);
          data.forEach((task, i) => {
            console.log('Created new task', task.id);
            newtaskids.push(task.id);
          })
        });
        this.props.fetchData(true);
      }
      else if (response.status == 400)
      {
        response.json().then(data => {
          console.log('New task: errors returned', data);
          this.setState({'errors': data});
        });
      }
      else
      {
        console.log("New task: Error on submission: ", response.status);
      };
    })
    .catch(error => {
      submission_in_progress = false;
      console.log('New task HTTP request failed', error);
      this.setState({
          'httperror': 'HTTP request failed. Check internet connection and server are online.',
          'submission_in_progress': false});
    });
  }

  onSubmit() {
    event.preventDefault();
    if (submission_in_progress) {
      console.log('New task: Submission already in progress');
      return;
    }

    console.log('New task: Submitting', api_url_base);
    submission_in_progress = true;
    this.setState({'submission_in_progress': false});
    this.submit();
  }

  render() {
    var formcontent = [];

    formcontent.push(
      <ul key="ulradec">
        <li><label htmlFor="id_radeclist">RA Dec / MPC names:</label>
        <textarea name="radeclist" cols="" rows="3" required id="id_radeclist" value={this.state.radeclist} onChange={e => {this.setState({'radeclist': e.target.value}); localStorage.setItem("radeclist", e.target.value);}}></textarea>
        &nbsp;<a onClick={()=> {this.setState({'showradechelp': !this.state.showradechelp})}}>Help</a>
        {this.state.showradechelp ? <div id="radec_help" style={{display: 'block', clear: 'right', fontSize: 'small'}} className="collapse">Each line should consist of a right ascension and a declination coordinate (J2000) in decimal or sexagesimal notation (RA/DEC separated by a space or a comma) or 'mpc ' and a Minor Planet Center object name (e.g. 'mpc Makemake'). Limit of 100 objects per submission. If requested, email notification will be sent only after all targets in the list have been processed.</div> : null}
        </li>
        {'radeclist' in this.state.errors ? <ul className="errorlist"><li>{this.state.errors['radeclist']}</li></ul> : ''}
      </ul>
    );

    formcontent.push(
      <div key="propermotion_checkbox" id="propermotion_checkboxdiv" style={{width: '100%'}}>
        <label style={{width: '100%'}}>
            <input type="checkbox" checked={this.state.enable_propermotion} onChange={e => {this.setState({'enable_propermotion': e.target.checked}); localStorage.setItem("enable_propermotion", e.target.checked);}} style={{position: 'static', display: 'inline', width: '5em'}} /> Proper motion
        </label>
      </div>);
      if (this.state.enable_propermotion) {
        formcontent.push(
          <div key="propermotion_panel" id="propermotion_panel" style={{background: 'rgb(235,235,235)'}}>
              <p key="propermotiondesc" style={{fontSize: 'small'}}>If the star is moving, the J2000 coordinates above are correct for a specified epoch along with proper motions in RA (angle) and Dec in milliarcseconds. The epoch of ATLAS observations varies from 2015.5 to the present. Note: these are angular velocities, not rates of coordinate change.</p>
              <ul key="propermotion_inputs">
                <li key="radec_epoch_year"><label htmlFor="id_radec_epoch_year">Epoch year:</label><input type="number" name="radec_epoch_year" step="0.1" id="id_radec_epoch_year" value={this.state.radec_epoch_year} onChange={e => {this.setState({'radec_epoch_year': e.target.value}); localStorage.setItem("radec_epoch_year", e.target.value);}} /></li>
                <li key="propermotion_ra"><label htmlFor="id_propermotion_ra">PM RA [mas/yr]</label><input type="number" name="propermotion_ra" step="any" id="id_propermotion_ra" value={this.state.propermotion_ra} onChange={e => {this.setState({'propermotion_ra': e.target.value}); localStorage.setItem("propermotion_ra", e.target.value);}} /></li>
                <li key="propermotion_dec"><label htmlFor="id_propermotion_dec">PM Dec [mas/yr]</label><input type="number" name="propermotion_dec" step="any" id="id_propermotion_dec" value={this.state.propermotion_dec} onChange={e => {this.setState({'propermotion_dec': e.target.value}); localStorage.setItem("propermotion_dec", e.target.value);}} /></li>
              </ul>
          </div>
        );
      }

    formcontent.push(
      <ul key="ulmjdoptions">
        <li key="mjd_min">
          <label htmlFor="id_mjd_min">MJD min:</label><input type="number" name="mjd_min" step="any" id="id_mjd_min" value={this.state.mjd_min} onChange={this.handlechange_mjd_min} />
          <a className="btn" onClick={() => {this.setState({'mjd_min': getDefaultMjdMin()}); this.update_mjd_min(getDefaultMjdMin()); localStorage.removeItem('mjd_min');}}>↩️</a>
          <p className="inputisodate" id='id_mjd_min_isoformat'>{this.state.mjd_min_isoformat}</p>
        </li>
        <li key="mjd_max">
          <label htmlFor="id_mjd_max">MJD max:</label><input type="number" name="mjd_max" step="any" id="id_mjd_max" value={this.state.mjd_max} onChange={this.handlechange_mjd_max} />
          <p className="inputisodate" id='id_mjd_max_isoformat'>{this.state.mjd_max_isoformat}</p>
          {'mjd_max' in this.state.errors ? <ul className="errorlist"><li>{this.state.errors['mjd_max']}</li></ul> : ''}
        </li>
        <li key="comment"><label htmlFor="id_comment">Comment:</label><input type="text" name="comment" maxLength="300" id="id_comment" value={this.state.comment} onChange={e => {this.setState({'comment': e.target.value}); localStorage.setItem("comment", e.target.value);}} /></li>

        <li key="use_reduced"><input type="checkbox" name="use_reduced" id="id_use_reduced" checked={this.state.use_reduced} onChange={e => {this.setState({'use_reduced': e.target.checked}); localStorage.setItem("use_reduced", e.target.checked);}} /><label htmlFor="id_use_reduced" >Use reduced (input) instead of difference images (<a href="../faq/">FAQ</a>)</label></li>
        <li key="send_email"><input type="checkbox" name="send_email" id="id_send_email" checked={this.state.send_email} onChange={e => {this.setState({'send_email': e.target.checked}); localStorage.setItem("send_email", e.target.checked);}}/><label htmlFor="id_send_email">Email me when completed</label></li>
      </ul>
    );

    var submitclassname = submission_in_progress ? 'btn btn-info submitting' : 'btn btn-info';
    var submitvalue = submission_in_progress ? 'Requesting...' : 'Request';

    formcontent.push(<input key="submitbutton" className={submitclassname} id="submitrequest" type="submit" value={submitvalue} />);
    if (this.state.httperror != '') {
      formcontent.push(<p key="httperror" style={{'color': 'red'}}>{this.state.httperror}</p>);
    }

    return (
      <div key="newrequestcontainer" id="newrequestcontainer">
        <div key="newrequestsource" className="newrequest" id="newrequestsource">
          <div key="newtask" className="task">
            <h2 key="newtaskheader">New request</h2>
            <form key="newtaskform" id="newrequest" onSubmit={this.onSubmit.bind(this)}>
              {formcontent}
            </form>
          </div>
        </div>
      </div>);
  }
}


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
    this.state = {}
    this.state.updateTimeElapsed = this.updateTimeElapsed.bind(this);
    this.state.interval = null;
    this.state.timeelapsed = -1;
  }

  deleteTask() {
    var li_id = '#task-' + this.props.taskdata.id
    // $(li_id).hide(300);
    $(li_id).slideUp(200);
    setTimeout(() => {
      // console.log('Starting delete of task ', this.props.taskdata.id);
      $.ajax({url: this.props.taskdata.url, method: 'delete',
        success: (result) => {console.log('Deleted task ', this.props.taskdata.id); this.props.fetchData()},
        error: (err) => { console.log('Failed to delete task ', this.props.taskdata.id, err); $('#task-' + this.props.taskdata.id).slideDown(100); this.props.fetchData();}
      });
    }, 200);
  }

  requestImages() {
    var request_image_url = new URL(this.props.taskdata.url);
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
        var newimgtask_id = parseInt(new URL(response.url).searchParams.get('newids'));
        newtaskids.push(newimgtask_id);
        console.log('requestimages created task', newimgtask_id);
        var new_page_url = new URL(response.url);
        new_page_url.searchParams.delete('newids');
        window.history.pushState({}, document.title, new_page_url);
        this.props.fetchData(true);
      }
    });
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
  // componentDidUpdate() {
    this.updateTimeElapsed();
    if (newtaskids.includes(this.props.taskdata.id)) {
      var li_id = '#task-' + this.props.taskdata.id
      console.log('showing new task', this.props.taskdata.id);
      $(li_id).hide();
      // $(li_id).show(600);
      $(li_id).slideDown(200);
      newtaskids = newtaskids.filter(item => {return item !== this.props.taskdata.id})
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
       delbutton = <button className="btn btn-sm btn-danger" onClick={() => this.deleteTask()}>{buttontext}</button>;
    }
    var taskbox = [
      <div key="rightside" className="rightside">
          {delbutton}
          <img src={task.previewimage_url} style={{display: 'block', marginTop: '1em', marginLeft: '1em'}} />
      </div>
    ];

    taskbox.push(<div key="tasknum"><a key="tasklink" href={task.url} onClick={(e) => {this.props.setSingleTaskView(e, task.id, task.url)}}>Task {task.id}</a></div>);

    if (task.parent_task_url) {
      taskbox.push(<p key="imgrequest">Image request for <a key="parent_task_link" href={task.parent_task_url} onClick={(e) => {this.props.setSingleTaskView(e, task.parent_task_id, task.parent_task_url)}}>Task {task.parent_task_id}</a></p>);
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

    if (task.mpc_name != null && task.mpc_name != '') {
      taskbox.push(<div key="target">MPC Object: {task.mpc_name}</div>);
    } else {
      var radecepoch = '';
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
        var mjdmin = task.mjd_min != null ? task.mjd_min : "0";
        var mjdmax = task.mjd_max != null ? task.mjd_max : "∞";
        taskbox.push(<div key="mjdrange">MJD request: [{mjdmin}, {mjdmax}]</div>);
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
          } else {
            taskbox.push(<p>The download link has expired. Delete this task and request again if necessary.</p>);
          }
        } else if (task.imagerequest_task_id != null) {
          if (task.imagerequest_finished) {
            taskbox.push(<a key="imgrequest" className="btn btn-primary" href={task.imagerequest_url} onClick={(e) => {this.props.setSingleTaskView(e, task.imagerequest_task_id, task.imagerequest_url)}}>Images retrieved</a>);
          } else {
            taskbox.push(<a key="imgrequest" className="btn btn-warning" href={task.imagerequest_url} onClick={(e) => {this.props.setSingleTaskView(e, task.imagerequest_task_id, task.imagerequest_url)}}>Images requested</a>);
          }
        } else if (user_id == task.user_id) {
            taskbox.push(<button key="imgrequest" className="btn btn-info" onClick={() => this.requestImages()} title="Download FITS and JPEG images for up to the first 500 observations.">Request {task.use_reduced ? 'reduced' : 'diff'} images</button>);
        }
      }
    } else if (task.starttimestamp != null) {
      taskbox.push(<div key="status" style={{color: 'red', fontStyle: 'italic', marginTop: '1em'}}>Running (started {this.state.timeelapsed} seconds ago)</div>);
    } else {
      taskbox.push(<div key="status" style={{fontStyle: 'italic', marginTop: '1em'}}>Waiting ({task.queuepos} tasks ahead of this one)</div>);
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
    var page_url = new URL(strurl);
    var started = page_url.searchParams.get('started');
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
    var new_page_url = new URL(api_url_base);
    new_page_url.search = '';
    if (filtername != null) {
      new_page_url.searchParams.set(filtername, true);
    }

    if (new_page_url != window.location.href) {
      window.history.pushState({}, document.title, new_page_url);
      var statechanges = {'scrollToTopAfterUpdate': true, dataurl: new_page_url};
      if (filtername == 'started') {
        if (this.state.results != null) {
          statechanges['results'] = this.state.results.filter(task => {return task.starttimestamp != null});
          if (statechanges['results'].length == 0) {
            // prevent flash of "there are no results" for empty ([] non-null) results list
            statechanges['results'] = null;
          }
        }
      }
      this.setState(statechanges, () => {this.fetchData(true)});
    }
  }

  singleTaskViewTaskId(strurl) {
    var pathext = strurl.toString().replace(
      api_url_base.toString(), '').split('/').filter(el => {return el.length != 0});

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
    var new_page_url = api_url_base + task_id + '/';
    window.history.pushState({}, document.title, new_page_url);

    console.log('Task list changed to single task view for ', new_page_url.toString());

    var newresults = this.state.results.filter(task => {return task.id == task_id});
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
    }, () => {this.fetchData(true)});
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

    this.setState({scrollToTopAfterUpdate: true}, () => {this.fetchData(true)});
  }

  fetchData(usertriggered) {
    if (document[hidden] || !user_is_active) {
      return;
    }

    this.setState({dataurl: window.location.href});

    // start by applying a cached version if we have it
    // then send out an HTTP request and update when available
    if (usertriggered)
    {
      var tasklist_fetchcachematch = (window.location.href in tasklist_fetchcache);
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
    var get_url = window.location.href;
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
          console.log("Fetch recieved HTTP status ", response.status);
        }
        if (response.status == 404) {
          window.history.pushState({}, document.title, api_url_base);
          this.setState({scrollToTopAfterUpdate: true}, () => {this.fetchData(true)});
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
      var statechanges = null;
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
      this.setState({scrollToTopAfterUpdate: false});
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
    var singletaskmode = this.singleTaskViewTaskId(this.state.dataurl) != null;
    var pagehtml = [];
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
      pagehtml.push(<NewRequest key="newrequest" fetchData={this.fetchData} />);
    }

    var tasklist;
    if (this.state.results == null) {
      tasklist = <p key="message">Loading tasks...</p>;
    } else if (this.state.results.length == 0) {
      tasklist = <p key="message">There are no tasks.</p>;
    } else {
      var pagetaskcount = (this.state.results != null) ? this.state.results.length : null;
      tasklist = [
        <ul key="ultasklist" className="tasks">
          {this.state.results.map((task) => (<Task key={task.id} taskdata={task} fetchData={this.fetchData} setSingleTaskView={this.setSingleTaskView} hidePlot={pagetaskcount > 10} />))}
        </ul>,
        <Pager key='pager' previous={this.state.previous} next={this.state.next} pagefirsttaskposition={this.state.pagefirsttaskposition} pagetaskcount={pagetaskcount} taskcount={this.state.taskcount} updateCursor={this.updateCursor} />
      ];
    }

    pagehtml.push(<div key="tasklist" id="tasklist" className={singletaskmode ? 'singletaskdetail' : null}>{tasklist}</div>);

    if (this.state.scrollToTopAfterUpdate) {
      window.scrollTo(0, 0);
      // this.setState({scrollToTopAfterUpdate: false});
      window.dispatchEvent(new Event('resize'));
    }

    return pagehtml;
  }
}


// ReactDOM.render(React.createElement(TaskPage, {}), document.getElementById('taskpage'));

const container = document.getElementById('taskpage');
const root = ReactDOM.createRoot(container);
root.render(<TaskPage />);
