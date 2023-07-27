'use strict';

let submission_in_progress = false;

function getDefaultMjdMin() {
    return (mjdFromDate(new Date()) - 30.).toFixed(5);
}

class NewRequest extends React.Component {
    get_defaultstate() {
        return {
            showradechelp: false,
            radeclist: localStorage.getItem('radeclist') != null ? localStorage.getItem('radeclist') : '',
            mjd_min: localStorage.getItem('mjd_min') != null ? localStorage.getItem('mjd_min') : getDefaultMjdMin(),
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
        let isostrmin = '';
        if (strmjdmin == '') {
            isostrmin = '(leave blank to fetch earliest)'
        } else {
            try {
                const mjdmin = parseFloat(strmjdmin);
                const isostr_withmilliseconds = dateFromMJD(mjdmin).toISOString();
                isostrmin = (
                    isostr_withmilliseconds.includes('.') ?
                        isostr_withmilliseconds.split('.')[0] + 'Z' : isostr_withmilliseconds);
            }
            catch (err) {
                isostrmin = 'error'
                console.log('error', err, err.message);
            }
        }
        this.setState({ 'mjd_min': strmjdmin, 'mjd_min_isoformat': isostrmin });
    }

    handlechange_mjd_min(event) {
        this.update_mjd_min(event.target.value);
        localStorage.setItem('mjd_min', event.target.value);
    }

    update_mjd_max(strmjdmax) {
        let isostrmax = '';
        if (strmjdmax == '') {
            isostrmax = '(leave blank to fetch latest)'
        } else {
            try {
                const mjdmax = parseFloat(strmjdmax);
                console.log("invalid?", strmjdmax, mjdmax);
                const isostr_withmilliseconds = dateFromMJD(mjdmax).toISOString();
                isostrmax = (
                    isostr_withmilliseconds.includes('.') ?
                        isostr_withmilliseconds.split('.')[0] + 'Z' : isostr_withmilliseconds);
            }
            catch (err) {
                isostrmax = 'error'
                console.log('error', err, err.message);
            }
        }
        this.setState({ 'mjd_max': strmjdmax, 'mjd_max_isoformat': isostrmax });
        localStorage.setItem('mjd_max', strmjdmax);
    }

    handlechange_mjd_max(event) {
        this.update_mjd_max(event.target.value);
    }

    async submit() {
        const datadict = {
            radeclist: this.state.radeclist,
            mjd_min: this.state.mjd_min == '' ? null : this.state.mjd_min,
            mjd_max: this.state.mjd_max == '' ? null : this.state.mjd_max,
            use_reduced: this.state.use_reduced,
            send_email: this.state.send_email,
            comment: this.state.comment,
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
                this.setState({ 'httperror': 'HTTP request failed.', 'submission_in_progress': false });
            })
            .then((response) => {
                submission_in_progress = false;
                this.setState({ 'httperror': '', 'submission_in_progress': false });
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
                    window.history.pushState({}, document.title, api_url_base);
                    this.props.fetchData(true);
                }
                else if (response.status == 400) {
                    response.json().then(data => {
                        console.log('New task: errors returned', data);
                        this.setState({ 'errors': data });
                    });
                }
                else {
                    console.log("New task: Error on submission: ", response.status);
                };
            })
            .catch(error => {
                submission_in_progress = false;
                console.log('New task HTTP request failed', error);
                this.setState({
                    'httperror': 'HTTP request failed. Check internet connection and server are online.',
                    'submission_in_progress': false
                });
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
        this.setState({ 'submission_in_progress': false });
        this.submit();
    }

    render() {
        const formcontent = [];

        formcontent.push(
            <ul key="ulradec">
                <li><label htmlFor="id_radeclist">RA Dec / MPC names:</label>
                    <textarea name="radeclist" cols="" rows="3" required id="id_radeclist" value={this.state.radeclist} onChange={e => { this.setState({ 'radeclist': e.target.value }); localStorage.setItem("radeclist", e.target.value); }}></textarea>
                    &nbsp;<a onClick={() => { this.setState({ 'showradechelp': !this.state.showradechelp }) }}>Help</a>
                    {this.state.showradechelp ? <div id="radec_help" style={{ display: 'block', clear: 'right', fontSize: 'small' }} className="collapse">Each line should consist of a right ascension and a declination coordinate (J2000) in decimal or sexagesimal notation (RA/DEC separated by a space or a comma) or 'mpc ' and a Minor Planet Center object name (e.g. 'mpc Makemake'). Limit of 100 objects per submission. If requested, email notification will be sent only after all targets in the list have been processed.</div> : null}
                </li>
                {'radeclist' in this.state.errors ? <ul className="errorlist"><li>{this.state.errors['radeclist']}</li></ul> : ''}
            </ul>
        );

        formcontent.push(
            <div key="propermotion_checkbox" id="propermotion_checkboxdiv" style={{ width: '100%' }}>
                <label style={{ width: '100%' }}>
                    <input type="checkbox" checked={this.state.enable_propermotion} onChange={e => { this.setState({ 'enable_propermotion': e.target.checked }); localStorage.setItem("enable_propermotion", e.target.checked); }} style={{ position: 'static', display: 'inline', width: '5em' }} /> Proper motion
                </label>
            </div>);
        if (this.state.enable_propermotion) {
            formcontent.push(
                <div key="propermotion_panel" id="propermotion_panel" style={{ background: 'rgb(235,235,235)' }}>
                    <p key="propermotiondesc" style={{ fontSize: 'small' }}>If the star is moving, the J2000 coordinates above are correct for a specified epoch along with proper motions in RA (angle) and Dec in milliarcseconds. The epoch of ATLAS observations varies from 2015.5 to the present. Note: these are angular velocities, not rates of coordinate change.</p>
                    <ul key="propermotion_inputs">
                        <li key="radec_epoch_year"><label htmlFor="id_radec_epoch_year">Epoch year:</label><input type="number" name="radec_epoch_year" step="0.1" id="id_radec_epoch_year" value={this.state.radec_epoch_year} onChange={e => { this.setState({ 'radec_epoch_year': e.target.value }); localStorage.setItem("radec_epoch_year", e.target.value); }} /></li>
                        <li key="propermotion_ra"><label htmlFor="id_propermotion_ra">PM RA [mas/yr]</label><input type="number" name="propermotion_ra" step="any" id="id_propermotion_ra" value={this.state.propermotion_ra} onChange={e => { this.setState({ 'propermotion_ra': e.target.value }); localStorage.setItem("propermotion_ra", e.target.value); }} /></li>
                        <li key="propermotion_dec"><label htmlFor="id_propermotion_dec">PM Dec [mas/yr]</label><input type="number" name="propermotion_dec" step="any" id="id_propermotion_dec" value={this.state.propermotion_dec} onChange={e => { this.setState({ 'propermotion_dec': e.target.value }); localStorage.setItem("propermotion_dec", e.target.value); }} /></li>
                    </ul>
                </div>
            );
        }

        formcontent.push(
            <ul key="ulmjdoptions">
                <li key="mjd_min">
                    <label htmlFor="id_mjd_min">MJD min:</label><input type="number" name="mjd_min" step="any" id="id_mjd_min" value={this.state.mjd_min} onChange={this.handlechange_mjd_min} />
                    <a className="btn" onClick={() => { this.setState({ 'mjd_min': getDefaultMjdMin() }); this.update_mjd_min(getDefaultMjdMin()); localStorage.removeItem('mjd_min'); }}>↩️</a>
                    <p className="inputisodate" id='id_mjd_min_isoformat'>{this.state.mjd_min_isoformat}</p>
                </li>
                <li key="mjd_max">
                    <label htmlFor="id_mjd_max">MJD max:</label><input type="number" name="mjd_max" step="any" id="id_mjd_max" value={this.state.mjd_max} onChange={this.handlechange_mjd_max} />
                    <p className="inputisodate" id='id_mjd_max_isoformat'>{this.state.mjd_max_isoformat}</p>
                    {'mjd_max' in this.state.errors ? <ul className="errorlist"><li>{this.state.errors['mjd_max']}</li></ul> : ''}
                </li>
                <li key="comment"><label htmlFor="id_comment">Comment:</label><input type="text" name="comment" maxLength="300" id="id_comment" value={this.state.comment} onChange={e => { this.setState({ 'comment': e.target.value }); localStorage.setItem("comment", e.target.value); }} /></li>

                <li key="use_reduced"><input type="checkbox" name="use_reduced" id="id_use_reduced" checked={this.state.use_reduced} onChange={e => { this.setState({ 'use_reduced': e.target.checked }); localStorage.setItem("use_reduced", e.target.checked); }} /><label htmlFor="id_use_reduced" >Use reduced (input) instead of difference images (<a href="../faq/">FAQ</a>)</label></li>
                <li key="send_email"><input type="checkbox" name="send_email" id="id_send_email" checked={this.state.send_email} onChange={e => { this.setState({ 'send_email': e.target.checked }); localStorage.setItem("send_email", e.target.checked); }} /><label htmlFor="id_send_email">Email me when completed</label></li>
            </ul>
        );

        const submitclassname = submission_in_progress ? 'btn btn-info submitting' : 'btn btn-info';
        const submitvalue = submission_in_progress ? 'Requesting...' : 'Request';

        formcontent.push(<input key="submitbutton" className={submitclassname} id="submitrequest" type="submit" value={submitvalue} />);
        if (this.state.httperror != '') {
            formcontent.push(<p key="httperror" style={{ 'color': 'red' }}>{this.state.httperror}</p>);
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

