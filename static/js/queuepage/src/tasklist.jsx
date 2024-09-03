'use strict';

const jslcdataglobal = new Object();
const jslabelsglobal = new Object();
const jslimitsglobal = new Object();

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
