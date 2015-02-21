import os
import claripy

from .stack_magic import Access, VarList
from .executable import Executable
from .sym_tracking import find_stack_tags
from .errors import FidgetError, FidgetUnsupportedError
from . import vexutils

import logging
l = logging.getLogger('fidget.patching')

class Fidget(object):
    def __init__(self, infile, safe=False, whitelist=None, blacklist=None, debugangr=False):
        self.infile = infile
        self.safe = safe
        self.whitelist = whitelist if whitelist is not None else []
        self.blacklist = blacklist if blacklist is not None else []
        self.error = False
        self._stack_patch_data = []

        self._binrepr = Executable(infile, debugangr)
        self._binrepr.safe = safe

    def apply_patches(self, outfile=None):
        patchdata = self.dump_patches()
        l.info('Accumulated %d patches, %d bytes of data', len(patchdata), sum(map(lambda x: len(x[1]), patchdata)))

        if outfile is None:
            outfile = self.infile + '.out'
        l.debug('Patching to %s', outfile)

        fin = open(self.infile)
        fout = open(outfile, 'w')

        buf = 'a'
        while buf:
            buf = fin.read(1024*1024)
            fout.write(buf)

        for offset, data in patchdata:
            fout.seek(offset)
            fout.write(data)
        fin.close()
        fout.close()
        os.chmod(outfile, 0755)
        l.debug('Patching complete!')

    def dump_patches(self):
        # TODO: More kinds of patches please :P
        return self._stack_patch_data

    def patch(self):
        self.patch_stack() # :(

    def patch_stack(self):
        l.debug('Patching function stacks')
        self._stack_patch_data = []

        # Loop through all the functions as found by angr's CFG
        funcs = self._binrepr.funcman.functions.keys()

        # Find the real _start on MIPS so we don't touch it
        do_not_touch = None
        if self._binrepr.angr.arch.name == 'MIPS32':
            for context in self._binrepr.cfg.get_all_nodes(self._binrepr.angr.entry):
                for succ, jumpkind in self._binrepr.cfg.get_successors_and_jumpkind(context):
                    if jumpkind == 'Ijk_Call':
                        do_not_touch = succ.addr
                        l.debug('Found MIPS entry point stub target %s', hex(do_not_touch))

        last_size = 0
        successes = 0
        for funcaddr in funcs:
            # But don't touch _start. Seriously.
            if funcaddr == self._binrepr.angr.entry:
                l.debug('Skipping entry point')
                continue

            # On MIPS there's another function that's part of the entry point.
            # Trying to mess with it will cause catastrope.
            if funcaddr == do_not_touch:
                l.debug('Skipping MIPS entry point stub target')
                continue

            # Only patch functions in the text section
            sec = self._binrepr.locate_physaddr(funcaddr)
            if sec is None or sec != 'text':
                l.debug('Skipping function 0x%x not in .text', funcaddr)
                continue

            # Check if the function is white/blacklisted
            # TODO: Do a real name lookup instead of a fake one
            funcname = 'sub_%x' % funcaddr
            if (len(self.whitelist) > 0 and funcname not in self.whitelist) or \
               (len(self.blacklist) > 0 and funcname in self.blacklist):
                l.debug('Function %s removed by whitelist/blacklist', funcname)
                continue

            l.info('Patching stack of %s', funcname)
            self.patch_function_stack(funcaddr)
            if len(self._stack_patch_data) > last_size:
                last_size = len(self._stack_patch_data)
                successes += 1
        if successes == 0:
            l.error('Could not patch any functions\' stacks!')
        else:
            l.info('Patched %d functions', successes)


    def patch_function_stack(self, funcaddr):
        clrp = claripy.ClaripyStandalone('fidget_function_%x' % funcaddr)
        clrp.unique_names = False
        symrepr = clrp.solver()
        alloc_op = None   # the instruction that performs a stack allocation
        dealloc_ops = []  # the instructions that perform a stack deallocation
        variables = VarList(self._binrepr, symrepr, 0)
        for tag, bindata in find_stack_tags(self._binrepr, symrepr, funcaddr):
            if tag == '': continue
            l.debug('Got a tag at 0x%0.8x: %s: %s', bindata.memaddr, tag, hex(bindata.value))

            if tag == 'STACK_ALLOC':
                if alloc_op is None:
                    alloc_op = bindata
                elif bindata.value < alloc_op.value:
                    alloc_op = bindata
                variables.stack_size = -alloc_op.value

            elif tag == 'STACK_DEALLOC':
                if not bindata.symval.symbolic:
                    continue
                dealloc_ops.append(bindata)

            elif tag == 'STACK_ACCESS':
                # This constructor adds itself to the variable tracker
                Access(bindata, variables, bindata.value < -variables.stack_size)

            elif tag == 'STACK_ALLOCA':
                l.warning('\tFunction appears to use alloca, abandoning')
                return

            else:
                raise FidgetUnsupportedError('You forgot to update the tag list, jerkface!')

        if alloc_op is None:
            l.info('\tFunction does not appear to have a stack frame (No alloc)')
            return

        if len(dealloc_ops) == 0:
            l.warning('\tFunction does not ever deallocate stack frame')

    # Find the lowest sp-access that isn't an argument to the next function
    # By starting at accesses to [esp] and stepping up a word at a time
        if self._binrepr.is_convention_stack_args():
            wordsize = self._binrepr.native_word
            i = variables.stack_size
            while True:
                if i in variables:
                    variables[i].special = True
                    i += wordsize
                else:
                    break

        num_vars = len(variables)
        if num_vars > 0:
            num_accs = variables.num_accesses()
            l.info('\tFunction has a stack frame of %s bytes', hex(variables.stack_size))
            l.info('\t%d access%s to %d address%s %s made.',
                num_accs, '' if num_accs == 1 else 'es',
                num_vars, '' if num_vars == 1 else 'es',
                'is' if num_accs == 1 else 'are')

            l.debug('Stack addresses: [%s]', ', '.join(hex(n) for n in variables.addr_list))
        else:
            l.info("\tFunction has 0x%x-byte stack frame, but doesn't use it for local vars", variables.stack_size)
            return

        variables.collapse()
        variables.mark_sizes()

        sym_stack_size = clrp.BitVec("stack_size", 64)
        symrepr.add(sym_stack_size >= variables.stack_size)
        symrepr.add(sym_stack_size <= variables.stack_size + (16 * len(variables) + 32))
        symrepr.add(sym_stack_size % (self._binrepr.native_word/8) == 0)

        alloc_op.apply_constraints(symrepr)
        symrepr.add(vexutils.SExtTo(64, alloc_op.symval) == -sym_stack_size)
        for op in dealloc_ops:
            op.apply_constraints(symrepr)
            symrepr.add(op.symval == 0)

        variables.old_size = variables.stack_size
        variables.stack_size = sym_stack_size
        variables.sym_link()

        # OKAY HERE WE GO
        #print '\nConstraints:'
        #vexutils.columnize(str(x) for x in symrepr.constraints)
        #print

        if not symrepr.satisfiable():
            l.critical('(%s) Safe constraints unsatisfiable, fix this NOW', hex(funcaddr))
            raise FidgetError("You're a terrible programmer")

        # FIXME: THIS is the bottleneck in patching right now. Can we do better?
        for constraint in variables.unsafe_constraints:
            if symrepr.satisfiable(extra_constraints=[constraint]):
                symrepr.add(constraint)
                l.debug('Added unsafe constraint:      %s', constraint)
            else:
                l.debug("DIDN'T add unsafe constraint: %s", constraint)

        new_stack = symrepr.any(variables.stack_size).value
        if new_stack == variables.old_size:
            l.warning('\tUnable to resize stack')
            return

        l.info('\tResized stack from 0x%x to 0x%x', variables.old_size, new_stack)

        for addr in variables.addr_list:
            fixedval = symrepr.any(variables.variables[addr].address)
            fixedval = self._binrepr.resign_int(fixedval.value, fixedval.size())
            l.debug('Moved %s (size %d) to %s', hex(addr), variables.variables[addr].size, hex(fixedval))

        self._stack_patch_data += alloc_op.get_patch_data(symrepr)
        for dealloc in dealloc_ops:
            dealloc.gotime = True
            self._stack_patch_data += dealloc.get_patch_data(symrepr)
        self._stack_patch_data += variables.get_patches()

