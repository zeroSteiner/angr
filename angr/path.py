from os import urandom
import copy
import logging
import collections
l = logging.getLogger("angr.path")

import simuvex
import claripy
import mulpyplexer

#pylint:disable=unidiomatic-typecheck

UNAVAILABLE_RET_ADDR = -1


class CallFrame(object):
    """
    Stores the address of the function you're in and the value of SP
    at the VERY BOTTOM of the stack, i.e. points to the return address.
    """
    def __init__(self, state=None, func_addr=None, stack_ptr=None, ret_addr=None, jumpkind=None):
        """
        Initialize with either a state or the function address,
        stack pointer, and return address
        """
        if state is not None:
            try:
                self.func_addr = state.se.any_int(state.ip)
                self.stack_ptr = state.se.any_int(state.regs.sp)
            except (simuvex.SimUnsatError, simuvex.SimSolverModeError):
                self.func_addr = None
                self.stack_ptr = None

            if state.arch.call_pushes_ret:
                self.ret_addr = state.memory.load(state.regs.sp, state.arch.bits/8, endness=state.arch.memory_endness, inspect=False)
            else:
                self.ret_addr = state.regs.lr

            # Try to convert the ret_addr to an integer
            try:
                self.ret_addr = state.se.any_int(self.ret_addr)
            except (simuvex.SimUnsatError, simuvex.SimSolverModeError):
                self.ret_addr = None
        else:
            self.func_addr = func_addr
            self.stack_ptr = stack_ptr
            self.ret_addr = ret_addr

        self.jumpkind = jumpkind if jumpkind is not None else (state.scratch.jumpkind if state is not None else None)
        self.block_counter = collections.Counter()

    def __str__(self):
        return "Func %#x, sp=%#x, ret=%#x" % (self.func_addr, self.stack_ptr, self.ret_addr)

    def __repr__(self):
        return '<CallFrame (Func %#x)>' % (self.func_addr)

    def copy(self):
        c = CallFrame(state=None, func_addr=self.func_addr, stack_ptr=self.stack_ptr, ret_addr=self.ret_addr,
                      jumpkind=self.jumpkind
                      )
        c.block_counter = collections.Counter(self.block_counter)
        return c


class CallStack(object):
    """
    Represents a call stack.
    """
    def __init__(self):
        self._callstack = []

    def __iter__(self):
        """
        Iterate through the callstack, from top to bottom
        (most recent first).
        """
        for cf in reversed(self._callstack):
            yield cf

    def push(self, cf):
        """
        Push the :class:`CallFrame` `cf` on the callstack.
        """
        self._callstack.append(cf)

    def pop(self):
        """
        Pops one :class:`CallFrame` from the callstack.

        :return: A CallFrame.
        """
        try:
            return self._callstack.pop(-1)
        except IndexError:
            raise ValueError("Empty CallStack")

    @property
    def top(self):
        """
        Returns the element at the top of the callstack without removing it.

        :return: A CallFrame.
        """
        try:
            return self._callstack[-1]
        except IndexError:
            raise ValueError("Empty CallStack")

    def __getitem__(self, k):
        """
        Returns the CallFrame at index k, indexing from the top of the stack.
        """
        k = -1 - k
        return self._callstack[k]

    def __len__(self):
        return len(self._callstack)

    def __repr__(self):
        return "<CallStack (depth %d)>" % len(self._callstack)

    def __str__(self):
        return "Backtrace:\n%s" % "\n".join(str(f) for f in self)

    def __eq__(self, other):
        if not isinstance(other, CallStack):
            return False

        if len(self) != len(other):
            return False

        for c1, c2 in zip(self._callstack, other._callstack):
            if c1.func_addr != c2.func_addr or c1.stack_ptr != c2.stack_ptr or c1.ret_addr != c2.ret_addr:
                return False

        return True

    def __ne__(self, other):
        return self != other

    def __hash__(self):
        return hash(tuple((c.func_addr, c.stack_ptr, c.ret_addr) for c in self._callstack))

    def copy(self):
        c = CallStack()
        c._callstack = [cf.copy() for cf in self._callstack]
        return c


class ReverseListProxy(list):
    def __iter__(self):
        return reversed(self)


class Path(object):
    """
    A Path represents a sequence of basic blocks for an execution of the program.

    :ivar name:     A string to identify the path.
    :ivar state:    The state of the program.
    :type state:    simuvex.SimState
    """
    def __init__(self, project, state, path=None):
        # this is the state of the path
        self.state = state
        self.errored = False

        # project
        self._project = project

        if path is None:
            # the path history
            self.history = PathHistory()
            self.callstack = CallStack()

            # Note that stack pointer might be symbolic, and simply calling state.se.any_int over sp will fail in that
            # case. We should catch exceptions here.
            try:
                stack_ptr = self.state.se.any_int(self.state.regs.sp)
            except (simuvex.SimSolverModeError, simuvex.SimUnsatError):
                stack_ptr = None

            self.callstack.push(CallFrame(state=None, func_addr=self.addr,
                                          stack_ptr=stack_ptr,
                                          ret_addr=UNAVAILABLE_RET_ADDR
                                          )
                                )
            self.popped_callframe = None
            self.callstack_backtrace = []

            # the previous run
            self.previous_run = None

            # A custom information store that will be passed to all its descendents
            self.info = {}

            # for merging
            self._upcoming_merge_points = []
            self._merge_flags = []
            self._merge_values = []
            self._merge_traces = []
            self._merge_addr_traces = []
            self._merge_depths = []

        else:
            # the path history
            self.history = PathHistory(path.history)
            self.callstack = path.callstack.copy()
            self.popped_callframe = path.popped_callframe
            self.callstack_backtrace = list(path.callstack_backtrace)

            # the previous run
            self.previous_run = path._run
            self.history._record_state(state)
            self.history._record_run(path._run)
            self._manage_callstack(state)

            # A custom information store that will be passed to all its descendents
            self.info = { k:copy.copy(v) for k, v in path.info.iteritems() }

            self._upcoming_merge_points = list(path._upcoming_merge_points)
            self._merge_flags = list(path._merge_flags)
            self._merge_values = list(path._merge_values)
            self._merge_traces = list(path._merge_traces)
            self._merge_addr_traces = list(path._merge_addr_traces)
            self._merge_depths = list(path._merge_depths)

        # for printing/ID stuff and inheritance
        self.name = str(id(self))
        self.path_id = urandom(8).encode('hex')

        # actual analysis stuff
        self._run_args = None       # sim_run args, to determine caching
        self._run = None
        self._run_error = None

    @property
    def addr(self):
        return self.state.se.any_int(self.state.regs.ip)

    @addr.setter
    def addr(self, val):
        self.state.regs.ip = val

    #
    # Pass-throughs to history
    #

    @property
    def length(self):
        return self.history.length

    @property
    def extra_length(self):
        return self.history.extra_length

    @property
    def weighted_length(self):
        return self.history.length + self.history.extra_length

    @property
    def jumpkind(self):
        return self.history._jumpkind

    @property
    def last_actions(self):
        return self.history.actions

    #
    # History traversal
    #

    @property
    def history_iterator(self):
        return HistoryIter(self.history)
    @property
    def addr_trace(self):
        return AddrIter(self.history)
    @property
    def trace(self):
        return RunstrIter(self.history)
    @property
    def targets(self):
        return TargetIter(self.history)
    @property
    def guards(self):
        return GuardIter(self.history)
    @property
    def jumpkinds(self):
        return JumpkindIter(self.history)
    @property
    def events(self):
        return EventIter(self.history)
    @property
    def actions(self):
        return ActionIter(self.history)

    def trim_history(self):
        self.history = self.history.copy()
        self.history._parent = None


    #
    # Stepping methods and successor access
    #

    def step(self, throw=None, **run_args):
        """
        Step a path forward. Optionally takes any argument applicable to project.factory.sim_run.

        :param jumpkind:          the jumpkind of the previous exit.
        :param addr an address:   to execute at instead of the state's ip.
        :param stmt_whitelist:    a list of stmt indexes to which to confine execution.
        :param last_stmt:         a statement index at which to stop execution.
        :param thumb:             whether the block should be lifted in ARM's THUMB mode.
        :param backup_state:      a state to read bytes from instead of using project memory.
        :param opt_level:         the VEX optimization level to use.
        :param insn_bytes:        a string of bytes to use for the block instead of #the project.
        :param max_size:          the maximum size of the block, in bytes.
        :param num_inst:          the maximum number of instructions.
        :param traceflags:        traceflags to be passed to VEX. Default: 0

        :returns:   An array of paths for the possible successors.
        """
        if self._run_args != run_args or not self._run:
            self._run_args = run_args
            self._make_sim_run(throw=throw)

        if self._run_error:
            return [ self.copy(error=self._run_error) ]

        out = [ Path(self._project, s, path=self) for s in self._run.flat_successors ]
        if 'insn_bytes' in run_args and 'addr' not in run_args and len(out) == 1 \
                and isinstance(self._run, simuvex.SimIRSB) \
                and self.addr + self._run.irsb.size == out[0].state.se.any_int(out[0].state.regs.ip):
            out[0].state.regs.ip = self.addr
        return out

    def clear(self):
        """
        This function clear the execution status.

        After calling this if you call :func:`step`, successors will be recomputed. If you changed something into path
        state you probably want to call this method.
        """
        self._run = None


    def _make_sim_run(self, throw=None):
        self._run = None
        self._run_error = None
        try:
            self._run = self._project.factory.sim_run(self.state, **self._run_args)
        except (AngrError, simuvex.SimError, claripy.ClaripyError) as e:
            l.debug("Catching exception", exc_info=True)
            self._run_error = e
            if throw:
                raise
        except (TypeError, ValueError, ArithmeticError, MemoryError) as e:
            l.debug("Catching exception", exc_info=True)
            self._run_error = e
            if throw:
                raise

    @property
    def next_run(self):
        # TODO: this should be documented.
        if self._run_error:
            return None
        if not self._run:
            raise AngrPathError("Please call path.step() before accessing next_run")
        return self._run

    @property
    def successors(self):
        if not (self._run_error or self._run):
            raise AngrPathError("Please call path.step() before accessing successors")
        return self.step(**self._run_args)

    @property
    def unconstrained_successors(self):
        if self._run_error:
            return []
        if not self._run:
            raise AngrPathError("Please call path.step() before accessing successors")
        return [ Path(self._project, s, path=self) for s in self._run.unconstrained_successors ]

    @property
    def unsat_successors(self):
        if self._run_error:
            return []
        if not self._run:
            raise AngrPathError("Please call path.step() before accessing successors")
        return [ Path(self._project, s, path=self) for s in self._run.unsat_successors ]

    @property
    def mp_successors(self):
        return mulpyplexer.MP(self.successors)

    @property
    def nonflat_successors(self):
        if self._run_error:
            return []
        if not self._run:
            raise AngrPathError("Please call path.step() before accessing successors")

        nonflat_successors = [ ]
        for s in self._run.successors + self._run.unconstrained_successors:
            sp = Path(self._project, s, path=self)
            nonflat_successors.append(sp)
        return nonflat_successors

    @property
    def unconstrained_successor_states(self):
        if self._run_error:
            return []
        if not self._run:
            raise AngrPathError("Please call path.step() before accessing successors")

        return self._run.unconstrained_successors

    #
    # Utility functions
    #

    def branch_causes(self):
        """
        Returns the variables that have caused this path to branch.

        :return: A list of tuples of (basic block address, jmp instruction address, set(variables))
        """
        return [
            (h.addr, h._jump_source, tuple(h._guard.variables)) for h in self.history_iterator
            if h._jump_avoidable
        ]

    def divergence_addr(self, other):
        """
        Returns the basic block at which the paths diverged.

        :param other: The other Path.
        :returns:     The address of the basic block.
        """

        trace1 = self.addr_trace.hardcopy
        trace2 = other.addr_trace.hardcopy
        for i in range(max([len(trace1), len(trace2)])):
            if i > len(trace1):
                return trace2[i-1]
            elif i > len(trace2):
                return trace1[i-1]
            elif trace1[i] != trace2[i]:
                return trace1[i-1]


    def detect_loops(self, n=None): #pylint:disable=unused-argument
        """
        Returns the current loop iteration that a path is on.

        :param n:   The minimum number of iterations to check for.
        :returns:   The number of the loop iteration it's in.
        """

        # TODO: make this work better
        #addr_strs = [ "%x"%x for x in self.addr_trace ]
        #bigstr = "".join(addr_strs)

        #candidates = [ ]

        #max_iteration_length = len(self.addr_trace) / n
        #for i in range(max_iteration_length):
        #   candidates.append("".join(addr_strs[-i-0:]))

        #for c in reversed(candidates):
        #   if bigstr.count(c) >= n:
        #       return n
        #return None

        mc = self.callstack.top.block_counter.most_common()
        if len(mc) == 0:
            return None
        else:
            return mc[0][1]

    #
    # Error checking
    #

    _jk_errors = set(("Ijk_EmFail", "Ijk_NoDecode", "Ijk_MapFail"))
    _jk_signals = set(('Ijk_SigILL', 'Ijk_SigTRAP', 'Ijk_SigSEGV', 'Ijk_SigBUS',
                       'Ijk_SigFPE_IntDiv', 'Ijk_SigFPE_IntOvf'))
    _jk_all_bad = _jk_errors | _jk_signals

    #
    # Convenience functions
    #

    @property
    def reachable(self):
        return self.history.reachable()

    @property
    def _s0(self):
        return self.step()[0]
    @property
    def _s1(self):
        return self.step()[1]
    @property
    def _s2(self):
        return self.step()[2]
    @property
    def _s3(self):
        return self.step()[3]

    #
    # State continuation
    #

    def _manage_callstack(self, state):
        """
        Adds the information from the last run to the current path.
        """

        # maintain the blockcounter stack
        if state.scratch.jumpkind == "Ijk_Call":
            callframe = CallFrame(state)
            self.callstack.push(callframe)
            self.callstack_backtrace.append((hash(self.callstack), callframe, len(self.callstack)))
        elif state.scratch.jumpkind.startswith('Ijk_Sys'):
            callframe = CallFrame(state)
            self.callstack.push(callframe)
            self.callstack_backtrace.append((hash(self.callstack), callframe, len(self.callstack)))
        elif state.scratch.jumpkind == "Ijk_Ret":
            self.popped_callframe = self.callstack.pop()
            if len(self.callstack) == 0:
                l.info("Path callstack unbalanced...")
                self.callstack.push(CallFrame(None, 0, 0, 0))

        self.callstack.top.block_counter[state.scratch.bbl_addr] += 1

    #
    # Merging and splitting
    #

    def unmerge(self):
        """
        Unmerges the state back into different possible states.
        """

        l.debug("Unmerging %s!", self)

        states = [ self.state ]

        for flag,values in zip(self._merge_flags, self._merge_values):
            l.debug("... processing %s with %d possibilities", flag, len(values))

            new_states = [ ]

            for v in values:
                for s in states:
                    s_copy = s.copy()
                    s_copy.add_constraints(flag == v)
                    new_states.append(s_copy)

            states = [ s for s in new_states if s.satisfiable() ]
            l.debug("... resulting in %d satisfiable states", len(states))

        new_paths = [ ]
        for s in states:
            s.simplify()

            p = Path(self._project, s, path=self)
            new_paths.append(p)
        return new_paths

    def merge(self, *others):
        """
        Returns a merger of this path with `*others`.
        """
        all_paths = list(others) + [ self ]
        if len(set(( o.addr for o in all_paths))) != 1:
            raise AngrPathError("Unable to merge paths.")

        # merge the state
        new_state, merge_flag, _ = self.state.merge(*[ o.state for o in others ])
        new_path = Path(self._project, new_state, path=self)

        addr_lists = [x.addr_trace.hardcopy for x in all_paths]

        # fix the traces
        for i, addrs in enumerate(zip(*addr_lists)):
            if len(frozenset(addrs)) != 1:
                divergence_index = i
                break
        else:
            # divergence either happens at the end of the shortest history or doesn't at all
            divergence_index = i + 1

        rewind_count = len(addr_lists[0]) - divergence_index
        common_ancestor = all_paths[0].history
        for _ in xrange(rewind_count):
            common_ancestor = common_ancestor._parent

        assert common_ancestor.addr == addr_lists[0][divergence_index-1]
        new_path.history = PathHistory(common_ancestor)
        new_path.history._runstr = "MERGE POINT: %s" % merge_flag
        new_path.length = divergence_index

        # reset the upcoming merge points
        new_path._upcoming_merge_points = []
        new_path._merge_flags.append(merge_flag)
        new_path._merge_values.append(list(range(len(all_paths))))
        new_path._merge_traces.append([o.trace.hardcopy for o in all_paths])
        new_path._merge_addr_traces.append(addr_lists)
        new_path._merge_depths.append(new_path.length)

        return new_path

    def copy(self, error=None):
        if error is None:
            p = Path(self._project, self.state.copy())
        else:
            p = ErroredPath(error, self._project, self.state.copy())

        p.history = self.history.copy()
        p.callstack = self.callstack.copy()
        p.callstack_backtrace = list(self.callstack_backtrace)
        p.popped_callframe = self.popped_callframe


        p.previous_run = self.previous_run
        p._run = self._run

        p.info = {k: copy.copy(v) for k, v in self.info.iteritems()}

        p._upcoming_merge_points = list(self._upcoming_merge_points)
        p._merge_flags = list(self._merge_flags)
        p._merge_values = list(self._merge_values)
        p._merge_traces = list(self._merge_traces)
        p._merge_addr_traces = list(self._merge_addr_traces)
        p._merge_depths = list(self._merge_depths)

        return p

    def filter_actions(self, block_addr=None, block_stmt=None, insn_addr=None, read_from=None, write_to=None):
        """
        Filter self.actions based on some common parameters.

        :param block_addr:  Only return actions generated in blocks starting at this address.
        :param block_stmt:  Only return actions generated in the nth statement of each block.
        :param insn_addr:   Only return actions generated in the assembly instruction at this address.
        :param read_from:   Only return actions that perform a read from the specified location.
        :param write_to:    Only return actions that perform a write to the specified location.

        Notes:
        If IR optimization is turned on, reads and writes may not occur in the instruction
        they originally came from. Most commonly, If a register is read from twice in the same
        block, the second read will not happen, instead reusing the temp the value is already
        stored in.

        Valid values for read_from and write_to are the string literals 'reg' or 'mem' (matching
        any read or write to registers or memory, respectively), any string (representing a read
        or write to the named register), and any integer (representing a read or write to the
        memory at this address).
        """
        if read_from is not None:
            if write_to is not None:
                raise ValueError("Can't handle read_from and write_to at the same time!")
            if read_from in ('reg', 'mem'):
                read_type = read_from
                read_offset = None
            elif isinstance(read_from, str):
                read_type = 'reg'
                read_offset = self._project.arch.registers[read_from][0]
            else:
                read_type = 'mem'
                read_offset = read_from
        if write_to is not None:
            if write_to in ('reg', 'mem'):
                write_type = write_to
                write_offset = None
            elif isinstance(write_to, str):
                write_type = 'reg'
                write_offset = self._project.arch.registers[write_to][0]
            else:
                write_type = 'mem'
                write_offset = write_to

        def addr_of_stmt(bbl_addr, stmt_idx):
            if stmt_idx is None:
                return None
            stmts = self._project.factory.block(bbl_addr).vex.statements
            if stmt_idx >= len(stmts):
                return None
            for i in reversed(xrange(stmt_idx + 1)):
                if stmts[i].tag == 'Ist_IMark':
                    return stmts[i].addr + stmts[i].delta
            return None

        def action_reads(action):
            if action.type != read_type:
                return False
            if action.action != 'read':
                return False
            if read_offset is None:
                return True
            addr = action.addr
            if isinstance(addr, simuvex.SimActionObject):
                addr = addr.ast
            if isinstance(addr, claripy.ast.Base):
                if addr.symbolic:
                    return False
                addr = self.state.se.any_int(addr)
            if addr != read_offset:
                return False
            return True

        def action_writes(action):
            if action.type != write_type:
                return False
            if action.action != 'write':
                return False
            if write_offset is None:
                return True
            addr = action.addr
            if isinstance(addr, simuvex.SimActionObject):
                addr = addr.ast
            if isinstance(addr, claripy.ast.Base):
                if addr.symbolic:
                    return False
                addr = self.state.se.any_int(addr)
            if addr != write_offset:
                return False
            return True

        return [x for x in reversed(self.actions) if
                    (block_addr is None or x.bbl_addr == block_addr) and
                    (block_stmt is None or x.stmt_idx == block_stmt) and
                    (read_from is None or action_reads(x)) and
                    (write_to is None or action_writes(x)) and
                    (insn_addr is None or (x.sim_procedure is None and addr_of_stmt(x.bbl_addr, x.stmt_idx) == insn_addr))
            ]

    def __repr__(self):
        return "<Path with %d runs (at 0x%x)>" % (self.length, self.addr)


class ErroredPath(Path):
    """
    ErroredPath is used for paths that have encountered and error in their symbolic execution. This kind of path cannot
    be stepped further.

    :ivar error:    The error that was encountered.
    """
    def __init__(self, error, *args, **kwargs):
        super(ErroredPath, self).__init__(*args, **kwargs)
        self.error = error
        self.errored = True

    def __repr__(self):
        return "<Errored Path with %d runs (at 0x%x, %s)>" % \
            (self.length, self.addr, type(self.error).__name__)

    def step(self, *args, **kwargs):
        # pylint: disable=unused-argument
        raise AngrPathError("Cannot step forward an errored path")

    def retry(self, **kwargs):
        self._run_args = kwargs
        self._run = self._project.factory.sim_run(self.state, **self._run_args)
        return super(ErroredPath, self).step(**kwargs)


def make_path(project, runs):
    """
    A helper function to generate a correct angr.Path from a list of runs corresponding to a program path.

    :param runs:    A list of SimRuns corresponding to a program path.
    """

    if len(runs) == 0:
        raise AngrPathError("Cannot generate Path from empty set of runs")

    # This creates a path which state is the the first run
    a_p = Path(project, runs[0].initial_state)
    # And records the first node's run
    a_p.history = PathHistory(a_p.history)
    a_p.history._record_run(runs[0])

    # We then go through all the nodes except the last one
    for r in runs[1:-1]:
        a_p.history._record_state(r.initial_state)
        a_p._manage_callstack(r.initial_state)
        a_p.history = PathHistory(a_p.history)
        a_p.history._record_run(r)

    # We record the last state and set it as current (it is the initial
    # state of the next run).
    a_p.history._record_state(runs[-1].initial_state)
    a_p._manage_callstack(runs[-1].initial_state)
    a_p.state = runs[-1].initial_state

    return a_p

from .errors import AngrError, AngrPathError
from .path_history import * #pylint:disable=wildcard-import,unused-wildcard-import
