import subprocess
import os
import os.path
import shutil
import sys
import datetime
import time
import re
import json
from ordereddict import OrderedDict
from distutils.version import StrictVersion

from tornado import ioloop
from tornado import gen
from tornado.httpclient import AsyncHTTPClient#, HTTPRequest

import seesaw
from seesaw.event import Event
from seesaw.externalprocess import AsyncPopen
from seesaw.runner import Runner
from seesaw.web import start_runner_server

URL = 'http://warriorhq.archiveteam.org/projects.json'

class Autorunner(object):
  def __init__(self, projects_dir, data_dir, downloader, concurrent_items, address, port):
    self.projects_dir = projects_dir
    self.data_dir = data_dir
    self.downloader = downloader
    self.concurrent_items = concurrent_items
    self.address = address
    self.port = port
    
    # disable the password prompts
    self.gitenv = dict( os.environ.items() + { 'GIT_ASKPASS': 'echo', 'SSH_ASKPASS': 'echo' }.items() )
    
    self.runner = Runner(concurrent_items=self.concurrent_items)
    self.runner.on_finish += self.handle_runner_finish
        
    self.current_project_name = None
    self.current_project = None
    
    self.selected_project = None
    
    self.projects = {}
    self.installed_projects = set()
    self.failed_projects = set()
    
    self.on_projects_loaded = Event()
    self.on_project_installing = Event()
    self.on_project_installed = Event()
    self.on_project_installation_failed = Event()
    self.on_project_refresh = Event()
    self.on_project_selected = Event()
    self.on_status = Event()
    
    self.http_client = AsyncHTTPClient()
    
    self.installing = False
    self.shut_down_flag = False
    
    self.hq_updater = ioloop.PeriodicCallback(self.update_projects, 10*60*1000)
    self.project_updater = ioloop.PeriodicCallback(self.update_project, 60*60*1000)
    self.forced_stop_timeout = None
  
  @gen.engine
  def update_projects(self):
    response = yield gen.Task(self.http_client.fetch, URL, method="GET")
    if response.code == 200:
      data = json.loads(response.body)
      if StrictVersion(seesaw.__version__) < StrictVersion(data["warrior"]["seesaw_version"]):
        # time for an update
        print "There's a new version of Seesaw, you should update."
        self.stop_gracefully()
        
        # schedule a forced reboot after two days
        self.schedule_forced_stop()
        return
      
      projects_list = data["projects"]
      self.projects = OrderedDict([ (project["name"], project) for project in projects_list ])
      for project_data in self.projects.itervalues():
        if "deadline" in project_data:
          project_data["deadline_int"] = time.mktime(time.strptime(project_data["deadline"], "%Y-%m-%dT%H:%M:%SZ"))
      
      if self.selected_project and not self.selected_project in self.projects:
        self.select_project(None)
      else:
        # ArchiveTeam's choice
        if "auto_project" in data:
          self.select_project(data["auto_project"])
        else:
          self.select_project(None)
      
      self.on_projects_loaded(self, self.projects)
    
    else:
      print "HTTP error %s" % (response.code)
  
  @gen.engine
  def install_project(self, project_name, callback=None):
    self.installed_projects.discard(project_name)
    
    if project_name in self.projects and not self.installing:
      self.installing = project_name
      self.install_output = []
      
      project = self.projects[project_name]
      project_path = os.path.join(self.projects_dir, project_name)
      
      self.on_project_installing(self, project)
      
      if project_name in self.failed_projects:
        if os.path.exists(project_path):
          shutil.rmtree(project_path)
        self.failed_projects.discard(project_name)
        
      if os.path.exists(project_path):
        subprocess.Popen(
            args=[ "git", "config", "remote.origin.url", project["repository"] ],
            cwd=project_path
        ).communicate()
        
        p = AsyncPopen(
            args=[ "git", "pull" ],
            cwd=project_path,
            env=self.gitenv
        )
      else:
        p = AsyncPopen(
            args=[ "git", "clone", project["repository"], project_path ],
            env=self.gitenv
        )
      p.on_output += self.collect_install_output
      p.on_end += yield gen.Callback("gitend")
      p.run()
      result = yield gen.Wait("gitend")
      
      if result != 0:
        self.install_output.append("\ngit returned %d\n" % result)
        self.on_project_installation_failed(self, project, "".join(self.install_output))
        self.installing = None
        self.failed_projects.add(project_name)
        if callback:
          callback(False)
        return
      
      project_install_file = os.path.join(project_path, "warrior-install.sh")
      
      if os.path.exists(project_install_file):
        p = AsyncPopen(
            args=[ project_install_file ],
            cwd=project_path
        )
        p.on_output += self.collect_install_output
        p.on_end += yield gen.Callback("installend")
        p.run()
        result = yield gen.Wait("installend")
        
        if result != 0:
          self.install_output.append("\nCustom installer returned %d\n" % result)
          self.on_project_installation_failed(self, project, "".join(self.install_output))
          self.installing = None
          self.failed_projects.add(project_name)
          if callback:
            callback(False)
          return
      
      data_dir = os.path.join(self.data_dir, "data")
      if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
      os.makedirs(data_dir)
      
      project_data_dir = os.path.join(project_path, "data")
      if os.path.islink(project_data_dir):
        os.remove(project_data_dir)
      elif os.path.isdir(project_data_dir):
        shutil.rmtree(project_data_dir)
      os.symlink(data_dir, project_data_dir)
      
      self.installed_projects.add(project_name)
      self.on_project_installed(self, project, "".join(self.install_output))
      
      self.installing = None
      if callback:
        callback(True)
  
  @gen.engine
  def update_project(self):
    if self.selected_project and (yield gen.Task(self.check_project_has_update, self.selected_project)):
      # restart project
      self.start_selected_project()
  
  @gen.engine
  def check_project_has_update(self, project_name, callback):
    if project_name in self.projects:
      project = self.projects[project_name]
      project_path = os.path.join(self.projects_dir, project_name)
      
      self.install_output = []
      
      if not os.path.exists(project_path):
        callback(True)
        return
        
      subprocess.Popen(
          args=[ "git", "config", "remote.origin.url", project["repository"] ],
          cwd=project_path
      ).communicate()
      
      p = AsyncPopen(
          args=[ "git", "fetch" ],
          cwd=project_path,
          env=self.gitenv
      )
      p.on_output += self.collect_install_output
      p.on_end += yield gen.Callback("gitend")
      p.run()
      result = yield gen.Wait("gitend")
      
      if result != 0:
        callback(True)
        return
      
      output = subprocess.Popen(
          args=[ "git", "rev-list", "HEAD..FETCH_HEAD" ],
          cwd=project_path,
          stdout=subprocess.PIPE
      ).communicate()[0]
      if output.strip() != "":
        callback(True)
      else:
        callback(False)
  
  def collect_install_output(self, data):
    sys.stdout.write(data)
    data = re.sub("[\x00-\x08\x0b\x0c]", "", data)
    self.install_output.append(data)
  
  @gen.engine
  def select_project(self, project_name):
    if project_name == "auto":
      self.update_projects()
      return
    
    if not project_name in self.projects:
      project_name = None
    
    if project_name != self.selected_project:
      # restart
      self.selected_project = project_name
      self.on_project_selected(self, project_name)
      self.start_selected_project()
  
  def clone_project(self, project_name, project_path):
    version_string = subprocess.Popen(
        args=[ "git", "log", "-1", "--pretty=%h" ],
        cwd=project_path,
        stdout=subprocess.PIPE
    ).communicate()[0].strip()
    
    project_versioned_path = os.path.join(self.data_dir, "projects", "%s-%s" % (project_name, version_string))
    if not os.path.exists(project_versioned_path):
      if not os.path.exists(os.path.join(self.data_dir, "projects")):
        os.makedirs(os.path.join(self.data_dir, "projects"))
      
      subprocess.Popen(
          args=[ "git", "clone", project_path, project_versioned_path ],
          env=self.gitenv
      ).communicate()
    
    return project_versioned_path
  
  def load_pipeline(self, pipeline_path, context):
    dirname, basename = os.path.split(pipeline_path)
    if dirname == "":
      dirname = "."
    
    with open(pipeline_path) as f:
      pipeline_str = f.read()
    
    local_context = context
    global_context = context
    curdir = os.getcwd()
    try:
      os.chdir(dirname)
      exec pipeline_str in local_context, global_context
    finally:
      os.chdir(curdir)
    
    return ( local_context["project"], local_context["pipeline"] )
  
  @gen.engine
  def start_selected_project(self):
    project_name = self.selected_project
    
    if project_name in self.projects:
      # install or update project if necessary
      if not project_name in self.installed_projects or (yield gen.Task(self.check_project_has_update, project_name)):
        result = yield gen.Task(self.install_project, project_name)
        if not result:
          return
      
      # the path with the project code
      # (this is the most recent code from the repository)
      project_path = os.path.join(self.projects_dir, project_name)
      
      # clone the project code to a versioned directory
      # where the pipeline is actually run
      project_versioned_path = self.clone_project(project_name, project_path)
      
      # load the pipeline from the versioned directory
      pipeline_path = os.path.join(project_versioned_path, "pipeline.py")
      (project, pipeline) = self.load_pipeline(pipeline_path, { "downloader": self.downloader })
      
      # start the pipeline
      if not self.shut_down_flag:
        self.runner.set_current_pipeline(pipeline)
      
      start_runner_server(project, self.runner, bind_address=self.address, port_number=self.port)
      
      self.current_project_name = project_name
      self.current_project = project
      
      self.on_project_refresh(self, self.current_project, self.runner)
      self.fire_status()
      
      if not self.shut_down_flag:
        self.runner.start()
    
    else:
      # project_name not in self.projects,
      # stop the current project (if there is one)
      self.runner.set_current_pipeline(None)
      self.fire_status()
  
  def handle_runner_finish(self, runner):
    self.current_project_name = None
    self.current_project = None
    
    self.on_project_refresh(self, self.current_project, self.runner)
    self.fire_status()
    
    if self.shut_down_flag:
      ioloop.IOLoop.instance().stop()
      
      if self.shut_down_flag:
        sys.exit()
  
  def start(self):
    self.hq_updater.start()
    self.project_updater.start()
    self.update_projects()
    ioloop.IOLoop.instance().start()
  
  def schedule_forced_stop(self):
    if not self.forced_stop_timeout:
      self.forced_stop_timeout = ioloop.IOLoop.instance().add_timeout(datetime.timedelta(days=2), self.forced_stop)
  
  def forced_stop(self):
    sys.exit(1)
  
  def stop_gracefully(self):
    self.shut_down_flag = True
    self.fire_status()
    if self.runner.is_active():
      self.runner.set_current_pipeline(None)
    else:
      ioloop.IOLoop.instance().stop()
      sys.exit()
  
  def keep_running(self):
    self.shut_down_flag = False
    self.start_selected_project()
    self.fire_status()
  
  class Status(object):
    NO_PROJECT = "NO_PROJECT"
    INVALID_SETTINGS = "INVALID_SETTINGS"
    STOPPING_PROJECT = "STOPPING_PROJECT"
    RESTARTING_PROJECT = "RESTARTING_PROJECT"
    RUNNING_PROJECT = "RUNNING_PROJECT"
    SWITCHING_PROJECT = "SWITCHING_PROJECT"
    STARTING_PROJECT = "STARTING_PROJECT"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    REBOOTING = "REBOOTING"
  
  def fire_status(self):
    self.on_status(self, self.warrior_status())
  
  def warrior_status(self):
    if self.shut_down_flag:
      return Autorunner.Status.SHUTTING_DOWN
    elif self.selected_project == None and self.current_project_name == None:
      return Autorunner.Status.NO_PROJECT
    elif self.selected_project:
      if self.selected_project == self.current_project_name:
        return Autorunner.Status.RUNNING_PROJECT
      else:
        return Autorunner.Status.STARTING_PROJECT
    else:
      return Autorunner.Status.STOPPING_PROJECT
  
