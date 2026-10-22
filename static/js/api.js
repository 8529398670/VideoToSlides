// Every call to the server goes through here, so the things that are easy to
// forget -- sending the cookie, attaching the CSRF token, turning a non-2xx
// response into a real Error -- happen once instead of at each call site.
const Api = {
  csrfToken: null,

  async request( path , options ) {
    const settings = options || {};
    const response = await fetch( path , {
      method: settings.method || "GET",
      // The session cookie is the only credential; without this an
      // authenticated request silently arrives as an anonymous one.
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: settings.body ? JSON.stringify( settings.body ) : undefined,
    } );

    let payload = {};
    try {
      payload = await response.json();
    } catch ( parseError ) {
      payload = {};
    }

    if ( !response.ok ) {
      const error = new Error( payload.error || response.statusText );
      error.status = response.status;
      throw error;
    }
    return payload;
  },

  // Any state-changing call carries the per-session CSRF token in the body.
  // Wrapping it here means a new endpoint cannot forget it.
  async post( path , body ) {
    return this.request( path , {
      method: "POST",
      body: Object.assign( {} , body || {} , { csrf_token: this.csrfToken } ),
    } );
  },

  async me() {
    try {
      const payload = await this.request( "/api/me" );
      this.csrfToken = payload.csrf_token;
      return payload;
    } catch ( error ) {
      if ( error.status === 401 ) return { authenticated: false };
      throw error;
    }
  },

  // The one POST with no CSRF token: there is no session yet to hold one.
  // The server guards it by requiring this JSON body instead -- see the
  // login route on the server side.
  login( credential ) {
    return this.request( "/api/login" , { method: "POST" , body: { credential: credential } } );
  },

  rename( displayName )        { return this.post( "/api/account/rename" , { display_name: displayName } ); },
  logout()                     { return this.post( "/api/logout" ); },
  listTeam()                   { return this.request( "/api/team" ); },
  listUsers()                  { return this.request( "/api/admin/users" ); },
  createUser( name , role )    { return this.post( "/api/admin/users" , { display_name: name , role: role } ); },
  reissueLogin( userId )       { return this.post( "/api/admin/users/" + userId + "/reissue-login" ); },
  setDisabled( userId , flag ) { return this.post( "/api/admin/users/" + userId + "/disabled" , { disabled: flag } ); },
  revokeAccess( userId )       { return this.post( "/api/admin/users/" + userId + "/revoke-access" ); },

  // API keys. createKey's expiresInDays is passed through as-is including null,
  // because the server reads absent as "use the default" and 0 as "never
  // expires" -- collapsing them here would lose the distinction.
  listKeys()                   { return this.request( "/api/keys" ); },
  createKey( name , role , expiresInDays ) {
    return this.post( "/api/keys" , { name: name , role: role , expires_in_days: expiresInDays } );
  },
  revokeKey( keyId )           { return this.post( "/api/keys/" + encodeURIComponent( keyId ) + "/revoke" ); },
  listAllKeys()                { return this.request( "/api/admin/keys" ); },
  revokeAnyKey( keyId )        { return this.post( "/api/admin/keys/" + encodeURIComponent( keyId ) + "/revoke" ); },

  // Uploads. A chunk is raw bytes rather than JSON, so the CSRF token rides
  // in a header instead of the body -- the server checks it the same way.
  // A 409 carries "received": where the server wants the next chunk to start.
  startUpload( filename , size , batch ) {
    return this.post( "/api/uploads" , { filename: filename , size: size , batch: batch || null } );
  },
  async uploadChunk( uploadId , offset , blob ) {
    const response = await fetch( "/api/uploads/" + encodeURIComponent( uploadId ) + "/chunk?offset=" + offset , {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/octet-stream" , "X-CSRF-Token": this.csrfToken || "" },
      body: blob,
    } );
    let payload = {};
    try { payload = await response.json(); } catch ( parseError ) { payload = {}; }
    if ( !response.ok ) {
      const error = new Error( payload.error || response.statusText );
      error.status = response.status;
      error.received = payload.received;
      throw error;
    }
    return payload;
  },
  completeUpload( uploadId )   { return this.post( "/api/uploads/" + encodeURIComponent( uploadId ) + "/complete" ); },
  abortUpload( uploadId )      { return this.post( "/api/uploads/" + encodeURIComponent( uploadId ) + "/abort" ); },

  addYoutube( url )            { return this.post( "/api/jobs/youtube" , { url: url } ); },

  // Jobs: always the caller's own.
  listJobs()                   { return this.request( "/api/jobs" ); },
  getJob( jobId )              { return this.request( "/api/jobs/" + jobId ); },
  cancelJob( jobId )           { return this.post( "/api/jobs/" + jobId + "/cancel" ); },
  retryJob( jobId )            { return this.post( "/api/jobs/" + jobId + "/retry" ); },
  deleteJob( jobId , deleteGoogle ) {
    return this.post( "/api/jobs/" + jobId + "/delete" , { delete_google: Boolean( deleteGoogle ) } );
  },
  setExcluded( jobId , n , excluded ) {
    return this.post( "/api/jobs/" + jobId + "/slides/" + n + "/exclude" , { excluded: excluded } );
  },
  // gslidesNew: make a separate Google deck instead of replacing the job's one.
  exportJob( jobId , pptx , gslides , gslidesNew ) {
    return this.post( "/api/jobs/" + jobId + "/export" , { pptx: pptx , gslides: gslides , gslides_new: Boolean( gslidesNew ) } );
  },

  // URLs rather than calls: these are opened by <img> and download links.
  // ?r= changes when a job is re-run, so the long image cache is never stale.
  thumbUrl( job , n )          { return "/api/jobs/" + job.id + "/thumbs/" + n + "?r=" + job.run; },
  slideUrl( job , n )          { return "/api/jobs/" + job.id + "/slides/" + n + "?r=" + job.run; },
  pptxUrl( job )               { return "/api/jobs/" + job.id + "/download/pptx"; },
  imagesUrl( job )             { return "/api/jobs/" + job.id + "/download/images"; },

  googleStatus()               { return this.request( "/api/google" ); },
  googleConnect()              { return this.post( "/api/google/connect" ); },
  googleDisconnect()           { return this.post( "/api/google/disconnect" ); },

  // Admin settings (app/core/settings.py). changes: { key: value | null },
  // where null puts a setting back to its default.
  getSettings()                { return this.request( "/api/settings" ); },
  saveSettings( changes )      { return this.post( "/api/settings" , { changes: changes } ); },
};
