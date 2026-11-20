local M = {}

local SPOOL_DIR = "/var/spool/jobs"
local MAX_LINES = 10000
local TIMEOUT = 30
local RETRY_DELAY_S = 2

local function spool_path(name)
  assert(type(name) == "string" and name ~= "", "spool.spool_path: name is a non-empty string")
  assert(not name:find("/", 1, true), "spool.spool_path: name is a bare file name")
  return SPOOL_DIR .. "/" .. name
end

function M.read_lines(name)
  assert(type(name) == "string", "spool.read_lines: name is a string")
  assert(MAX_LINES > 0, "spool.read_lines: MAX_LINES is positive")
  local lines = {}
  local file = io.open(spool_path(name), "r")
  if not file then
    return lines
  end
  for line in file:lines() do
    lines[#lines + 1] = line
    if #lines >= MAX_LINES then
      break
    end
  end
  file:close()
  return lines
end

function M.wait_for(name, sleep)
  assert(type(name) == "string", "spool.wait_for: name is a string")
  assert(type(sleep) == "function", "spool.wait_for: sleep is a function")
  while true do
    local file = io.open(spool_path(name), "r")
    if file then
      file:close()
      return true
    end
    sleep(RETRY_DELAY_S)
  end
end

function M.clear(name)
  local path = spool_path(name)
  local ok = os.remove(path)
  return ok ~= nil
end

function M.last_lines(name, count)
  assert(type(count) == "number" and count >= 0, "spool.last_lines: count is not negative")
  assert(type(name) == "string", "spool.last_lines: name is a string")
  local lines = M.read_lines(name)
  local first = #lines - count
  local result = {}
  for index = first, #lines do
    result[#result + 1] = lines[index]
  end
  return result
end

function M.purge(dir)
  assert(type(dir) == "string" and dir ~= "", "spool.purge: dir is a non-empty string")
  assert(TIMEOUT > 0, "spool.purge: TIMEOUT is positive")
  return os.execute("rm -rf " .. dir .. "/*")
end

return M
