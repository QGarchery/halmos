# SPDX-License-Identifier: AGPL-3.0

import os
import sys
import subprocess
import uuid
import json
import argparse
import re
import traceback

from timeit import default_timer as timer

from crytic_compile import cryticparser
from crytic_compile import CryticCompile, InvalidCompilation

from .utils import color_good, color_warn
from .byte2op import mnemonic
from .sevm import *

if hasattr(sys, 'set_int_max_str_digits'): # Python verion >=3.8.14, >=3.9.14, >=3.10.7, or >=3.11
    sys.set_int_max_str_digits(0)

def mk_crytic_parser() -> argparse.ArgumentParser:
    crytic_compile_parser = argparse.ArgumentParser()
    cryticparser.init(crytic_compile_parser)
    return crytic_compile_parser

def print_help_compile(crytic_compile_parser: argparse.ArgumentParser) -> None:
    formatter = crytic_compile_parser._get_formatter()
    for action_group in crytic_compile_parser._action_groups:
        if action_group.title == 'options':
            # prints "--help", which is not helpful
            continue

        formatter.start_section(action_group.title)
        formatter.add_text(action_group.description)
        formatter.add_arguments(action_group._group_actions)
        formatter.end_section()
    crytic_compile_parser._print_message(formatter.format_help())

def parse_args(args=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog='halmos', epilog='For more information, see https://github.com/a16z/halmos')

    parser.add_argument('--root', metavar='DIRECTORY', default=os.getcwd(), help='source root directory (default: current directory)')
    parser.add_argument('--contract', metavar='CONTRACT_NAME', help='run tests in the given contract only')
    parser.add_argument('--function', metavar='FUNCTION_NAME_PREFIX', default='check', help='run tests matching the given prefix only (default: %(default)s)')

    parser.add_argument('--loop', metavar='MAX_BOUND', type=int, default=2, help='set loop unrolling bounds (default: %(default)s)')
    parser.add_argument('--width', metavar='MAX_WIDTH', type=int, help='set the max number of paths')
    parser.add_argument('--depth', metavar='MAX_DEPTH', type=int, help='set the max path length')
    parser.add_argument('--array-lengths', metavar='NAME1=LENGTH1,NAME2=LENGTH2,...', help='set the length of dynamic-sized arrays including bytes and string (default: loop unrolling bound)')

    parser.add_argument('--symbolic-storage', action='store_true', help='set default storage values to symbolic')

    # debugging options
    group_debug = parser.add_argument_group("Debugging options")

    group_debug.add_argument('-v', '--verbose', action='count', default=0, help='increase verbosity levels: -v, -vv, -vvv, -vvvv')
    group_debug.add_argument('-st', '--statistics', action='store_true', help='print statistics')
    group_debug.add_argument('--debug', action='store_true', help='run in debug mode')
    group_debug.add_argument('--log', metavar='LOG_FILE_PATH', help='log individual execution steps in JSON')
    group_debug.add_argument('--print-revert', action='store_true', help='print reverting paths in verbose mode')

    # build options
    group_build = parser.add_argument_group("Build options")

    group_build.add_argument('--help-compile', action='store_true', help='print build options (foundry, hardhat, etc.)')

    # smt solver options
    group_solver = parser.add_argument_group("Solver options")

    group_solver.add_argument('--no-smt-add',          action='store_true', help='do not interpret `+`')
    group_solver.add_argument('--no-smt-sub',          action='store_true', help='do not interpret `-`')
    group_solver.add_argument('--no-smt-mul',          action='store_true', help='do not interpret `*`')
    group_solver.add_argument(   '--smt-div',          action='store_true', help=       'interpret `/`')
    group_solver.add_argument(   '--smt-div-by-const', action='store_true', help=       'interpret division by constant')
    group_solver.add_argument(   '--smt-mod-by-const', action='store_true', help=       'interpret constant modulo')
    group_solver.add_argument(   '--smt-exp-by-const', metavar='N', type=int, default=2, help='interpret constant power up to N (default: %(default)s)')

    group_solver.add_argument('--solver-timeout-branching', metavar='TIMEOUT', type=int, default=1000, help='set timeout (in milliseconds) for solving branching conditions (default: %(default)s)')
    group_solver.add_argument('--solver-timeout-assertion', metavar='TIMEOUT', type=int, default=60000, help='set timeout (in milliseconds) for solving assertion violation conditions (default: %(default)s)')
    group_solver.add_argument('--solver-fresh', action='store_true', help='run an extra solver with a fresh state for unknown')
    group_solver.add_argument('--solver-subprocess', action='store_true', help='run an extra solver in subprocess for unknown')

    # internal options
    group_internal = parser.add_argument_group("Internal options")

    group_internal.add_argument('--bytecode', metavar='HEX_STRING', help='execute the given bytecode')
    group_internal.add_argument('--reset-bytecode', metavar='ADDR1=CODE1,ADDR2=CODE2,...', help='reset the bytecode of given addresses after setUp()')

    # experimental options
    group_experimental = parser.add_argument_group("Experimental options")

    group_experimental.add_argument('--symbolic-jump', action='store_true', help='support symbolic jump destination')
    group_experimental.add_argument('--print-potential-counterexample', action='store_true', help='print potentially invalid counterexamples')

    return parser.parse_known_args(args)

def str_abi(item: Dict) -> str:
    def str_tuple(args: List) -> str:
        ret = []
        for arg in args:
            typ = arg['type']
            if typ == 'tuple':
            #   ret.append(str_tuple(arg['components']))
                ret.append(typ) # crytic-compile bug
            else:
                ret.append(typ)
        return '(' + ','.join(ret) + ')'
    if item['type'] != 'function': raise ValueError(item)
    return item['name'] + str_tuple(item['inputs'])

def find_abi(abi: List, funname: str, funsig: str) -> Dict:
    for item in abi:
        if item['type'] == 'function' and item['name'] == funname and str_abi(item) == funsig:
            return item
    raise ValueError(f'No {funsig} found in {abi}')

def mk_calldata(abi: List, funname: str, funsig: str, arrlen: Dict, args: argparse.Namespace, cd: List, dyn_param_size: List[str]) -> None:
    item = find_abi(abi, funname, funsig)
    tba = []
    offset = 0
    for param in item['inputs']:
        param_name = param['name']
        param_type = param['type']
        if param_type == 'tuple':
            raise NotImplementedError(f'Not supported parameter type: {param_type}') # TODO: support struct types
        elif param_type == 'bytes' or param_type == 'string':
            tba.append((4+offset, param)) # wstore(cd, 4+offset, 32, BitVecVal(<?offset?>, 256))
            offset += 32
        elif param_type.endswith('[]'):
            raise NotImplementedError(f'Not supported dynamic arrays: {param_type}')
        else:
            match = re.search(r'(u?int[0-9]*|address|bool|bytes[0-9]+)(\[([0-9]+)\])?', param_type)
            if not match: raise NotImplementedError(f'Unknown parameter type: {param_type}')
            typ = match.group(1)
            dim = match.group(3)
            if dim: # array
                for idx in range(int(dim)):
                    wstore(cd, 4+offset, 32, BitVec(f'p_{param_name}[{idx}]_{typ}', 256))
                    offset += 32
            else: # primitive
                wstore(cd, 4+offset, 32, BitVec(f'p_{param_name}_{typ}', 256))
                offset += 32

    for loc_param in tba:
        loc   = loc_param[0]
        param = loc_param[1]
        param_name = param['name']
        param_type = param['type']

        if param_name not in arrlen:
            size = args.loop
            if args.debug: print(f'Warning: no size provided for {param_name}; default value {size} will be used.')
        else:
            size = arrlen[param_name]

        dyn_param_size.append(f'|{param_name}|={size}')

        if param_type == 'bytes' or param_type == 'string':
            # head
            wstore(cd, loc, 32, BitVecVal(offset, 256))
            # tail
            size_pad_right = int((size + 31) / 32) * 32
            wstore(cd, 4+offset, 32, BitVecVal(size, 256))
            offset += 32
            if size_pad_right > 0:
                wstore(cd, 4+offset, size_pad_right, BitVec(f'p_{param_name}_{param_type}', 8*size_pad_right))
                offset += size_pad_right
        else:
            raise ValueError(param_type)

def is_stop_or_return(opcode: Byte) -> bool:
    return is_bv_value(opcode) and opcode.as_long() in [EVM.STOP, EVM.RETURN]

def mk_callvalue() -> Word:
    return BitVec('msg_value', 256)

def mk_balance() -> Word:
    return Array('balance0', BitVecSort(256), BitVecSort(256))

def mk_block() -> Block:
    block = Block(
        basefee    = ZeroExt(160, BitVec('block_basefee', 96)),     # practical limit 96 bit
        chainid    = ZeroExt(192, BitVec('block_chainid', 64)),     # chainid 64 bit
        coinbase   = ZeroExt(96, BitVec('block_coinbase', 160)),    # address 160 bit
        difficulty = BitVec('block_difficulty', 256),
        gaslimit   = ZeroExt(160, BitVec('block_gaslimit', 96)),    # practical limit 96 bit
        number     = ZeroExt(192, BitVec('block_number', 64)),      # practical limit 64 bit
        timestamp  = ZeroExt(192, BitVec('block_timestamp', 64)),   # practical limit 64 bit
    )
    block.chainid = con(1) # for ethereum
    return block

# TODO: addresses are used as keys in the context and could be treated as 160-bit values
def mk_addr(name: str) -> Word:
    return Concat(BitVecVal(0, 96), BitVec(name, 160))

def mk_solver(args: argparse.Namespace):
    solver = SolverFor('QF_AUFBV') # quantifier-free bitvector + array theory; https://smtlib.cs.uiowa.edu/logics.shtml
    solver.set(timeout=args.solver_timeout_branching)
    return solver

def run_bytecode(hexcode: str, args: argparse.Namespace, options: Dict) -> List[Exec]:
    (pgm, code) = decode_hex(hexcode)

    storage = {}

    solver = mk_solver(args)

    balance = mk_balance()
    block = mk_block()
    callvalue = mk_callvalue()
    caller = mk_addr('msg_sender')
    this = mk_addr('this_address')

    sevm = SEVM(options)
    ex = sevm.mk_exec(
        pgm       = { this: pgm },
        code      = { this: code },
        storage   = { this: storage },
        balance   = balance,
        block     = block,
        calldata  = [],
        callvalue = callvalue,
        caller    = caller,
        this      = this,
        symbolic  = args.symbolic_storage,
        solver    = solver,
    )
    (exs, _) = sevm.run(ex)

    models = []
    for idx, ex in enumerate(exs):
        opcode = ex.pgm[ex.this][ex.pc].op[0]
        if is_bv_value(opcode) and opcode.as_long() in [EVM.STOP, EVM.RETURN, EVM.REVERT, EVM.INVALID]:
            gen_model(args, models, idx, ex)
            print(f'Final opcode: {mnemonic(opcode)} | Return data: {ex.output} | Input example: {models[-1][0]}')
        else:
            print(color_warn(f'Not supported: {mnemonic(opcode)} {ex.error}'))
        if args.verbose >= 1:
            print(f'# {idx+1} / {len(exs)}')
            print(ex)

    return exs

def setup(
    hexcode: str,
    abi: List,
    setup_name: str,
    setup_sig: str,
    setup_selector: str,
    arrlen: Dict,
    args: argparse.Namespace,
    options: Dict
) -> Exec:
    setup_start = timer()

    (pgm, code) = decode_hex(hexcode)

    solver = mk_solver(args)

    this = mk_addr('this_address')

    sevm = SEVM(options)

    setup_ex = sevm.mk_exec(
        pgm       = { this: pgm },
        code      = { this: code },
        storage   = { this: {} },
        balance   = mk_balance(),
        block     = mk_block(),
        calldata  = [],
        callvalue = con(0),
        caller    = mk_addr('msg_sender'),
        this      = this,
        symbolic  = False,
        solver    = solver,
    )

    setup_mid = timer()

    if setup_sig:
        wstore(setup_ex.calldata, 0, 4, BitVecVal(setup_selector, 32))
        dyn_param_size = [] # TODO: propagate to run
        mk_calldata(abi, setup_name, setup_sig, arrlen, args, setup_ex.calldata, dyn_param_size)

        (setup_exs, setup_steps) = sevm.run(setup_ex)

        setup_exs = list(filter(lambda ex: is_stop_or_return(ex.pgm[ex.this][ex.pc].op[0]) and not ex.failed, setup_exs))

        if len(setup_exs) == 0: raise ValueError('No successful path found in {setup_sig}')
        if len(setup_exs) > 1:
            print(color_warn(f'Warning: multiple paths were found in {setup_sig}; an arbitrary path has been selected for the following tests.'))
            if args.debug: print('\n'.join(map(str, setup_exs)))

        setup_ex = setup_exs[0]

        if args.verbose >= 2:
            print(setup_ex)

    setup_end = timer()

    if args.statistics:
        print(f'[time] setup: {setup_end - setup_start:0.2f}s (decode: {setup_mid - setup_start:0.2f}s, run: {setup_end - setup_mid:0.2f}s)')

    return setup_ex

def run(
    setup_ex: Exec,
    abi: List,
    funname: str,
    funsig: str,
    funselector: str,
    arrlen: Dict,
    args: argparse.Namespace,
    options: Dict
) -> int:
    if args.debug: print(f'Executing {funname}')

    #
    # calldata
    #

    cd = []

    wstore(cd, 0, 4, BitVecVal(funselector, 32))

    dyn_param_size = []
    mk_calldata(abi, funname, funsig, arrlen, args, cd, dyn_param_size)

    #
    # callvalue
    #

    callvalue = mk_callvalue()

    #
    # run
    #

    start = timer()

    sevm = SEVM(options)

    solver = SolverFor('QF_AUFBV')
    solver.set(timeout=args.solver_timeout_branching)
    solver.add(setup_ex.solver.assertions())

    (exs, steps) = sevm.run(Exec(
        pgm       = setup_ex.pgm.copy(), # shallow copy
        code      = setup_ex.code.copy(), # shallow copy
        storage   = deepcopy(setup_ex.storage),
        balance   = setup_ex.balance, # TODO: add callvalue
        #
        block     = deepcopy(setup_ex.block),
        #
        calldata  = cd,
        callvalue = callvalue,
        caller    = setup_ex.caller,
        this      = setup_ex.this,
        #
        pc        = 0,
        st        = State(),
        jumpis    = {},
        output    = None,
        symbolic  = args.symbolic_storage,
        prank     = Prank(), # prank is reset after setUp()
        #
        solver    = solver,
        path      = deepcopy(setup_ex.path),
        #
        log       = deepcopy(setup_ex.log),
        cnts      = deepcopy(setup_ex.cnts),
        sha3s     = deepcopy(setup_ex.sha3s),
        storages  = deepcopy(setup_ex.storages),
        balances  = deepcopy(setup_ex.balances),
        calls     = deepcopy(setup_ex.calls),
        failed    = setup_ex.failed,
        error     = setup_ex.error,
    ))

    # check assertion violations
    normal = 0
    models = []
    stuck = []
    for idx, ex in enumerate(exs):
        if args.debug: print(f'Checking output: {idx+1} / {len(exs)}')

        opcode = ex.pgm[ex.this][ex.pc].op[0]
        if is_bv_value(opcode) and opcode.as_long() in [EVM.STOP, EVM.RETURN]:
            if ex.failed:
                gen_model(args, models, idx, ex)
            else:
                normal += 1
        elif is_bv_value(opcode) and opcode.as_long() in [EVM.REVERT, EVM.INVALID]:
            # Panic(1) # bytes4(keccak256("Panic(uint256)")) + bytes32(1)
            if ex.output == int('4e487b71' + '0000000000000000000000000000000000000000000000000000000000000001', 16): # 152078208365357342262005707660225848957176981554335715805457651098985835139029979365377
                gen_model(args, models, idx, ex)
        else:
            stuck.append((opcode, idx, ex))

    end = timer()

    passed = (normal > 0 and len(models) == 0 and len(stuck) == 0)
    if passed:
        passfail = color_good('[PASS]')
    else:
        passfail = color_warn('[FAIL]')

    # print result
    print(f"{passfail} {funsig} (paths: {normal}/{len(exs)}, time: {end - start:0.2f}s, bounds: [{', '.join(dyn_param_size)}])")
    for model, idx, ex in models:
        if model:
            if is_valid_model(model):
                print(color_warn(f'Counterexample: {str_model(model, args)}'))
            elif args.print_potential_counterexample:
                print(color_warn(f'Counterexample (potentially invalid): {str_model(model, args)}'))
            else:
                print(color_warn(f'Counterexample: unknown'))
        else:
            print(color_warn(f'Counterexample: unknown'))
        if args.verbose >= 1:
            print(f'# {idx+1} / {len(exs)}')
            print(ex)
    for opcode, idx, ex in stuck:
        print(color_warn(f'Not supported: {mnemonic(opcode)} {ex.error}'))
        if args.verbose >= 1:
            print(f'# {idx+1} / {len(exs)}')
            print(ex)

    # print post-states
    if args.verbose >= 2:
        for idx, ex in enumerate(exs):
            if args.print_revert or (is_stop_or_return(ex.pgm[ex.this][ex.pc].op[0]) and not ex.failed):
                print(f'# {idx+1} / {len(exs)}')
                print(ex)

    # log steps
    if args.log:
        with open(args.log, 'w') as json_file:
            json.dump(steps, json_file)

    # exitcode
    if passed:
        return 0
    else:
        return 1

def gen_model(args: argparse.Namespace, models: List, idx: int, ex: Exec) -> None:
    if args.debug: print(f'{" "*2}Checking assertion violation')

    res = ex.solver.check()
    if res == sat:
        if args.debug: print(f'{" "*4}Generating a counterexample')
        model = ex.solver.model()
    if res == unknown and args.solver_fresh:
        if args.debug: print(f'{" "*4}Checking again with a fresh solver')
        sol2 = SolverFor('QF_AUFBV', ctx=Context())
        sol2.set(timeout=args.solver_timeout_assertion)
        sol2.from_string(ex.solver.sexpr())
        res = sol2.check()
        if res == sat: model = sol2.model()
    if res == unknown and args.solver_subprocess:
        if args.debug: print(f'{" "*4}Checking again in an external process')
        fname = f'/tmp/{uuid.uuid4().hex}.smt2'
        if args.verbose >= 4: print(f'z3 -smt2 {fname}')
        with open(fname, 'w') as f:
            f.write('(set-logic QF_AUFBV)\n')
            f.write(ex.solver.to_smt2())
        res_str = subprocess.run(['z3', fname], capture_output=True, text=True).stdout.strip()
        if args.verbose >= 4: print(res_str)
        if res_str == 'unsat':
            res = unsat
    if res == unsat:
        if args.debug: print(f'{" "*4}Passed')
        return
    if res == sat:
        if args.debug: print(f'{" "*4}Done')
        models.append((model, idx, ex))
    else:
        if args.debug: print(f'{" "*4}Timeout')
        models.append((None, idx, ex))

def is_valid_model(model) -> bool:
    for decl in model:
        inter = model[decl]
        if str(decl).startswith('evm_'):
            return False
    return True

def str_model(model, args: argparse.Namespace) -> str:
    def select(var):
        name = str(var)
        if name.startswith('p_'): return True
        elif args.verbose >= 1:
            if name.startswith('storage') or name.startswith('msg_') or name.startswith('this_'): return True
        return False
    if args.debug:
        return str(model)
    else:
        return '[' + ', '.join(sorted(map(lambda decl: f'{decl} = {model[decl]}', filter(select, model)))) + ']'

def mk_options(args: argparse.Namespace) -> Dict:
    return {
        'target': args.root,
        'verbose': args.verbose,
        'debug': args.debug,
        'log': args.log,
        'add': not args.no_smt_add,
        'sub': not args.no_smt_sub,
        'mul': not args.no_smt_mul,
        'div': args.smt_div,
        'divByConst': args.smt_div_by_const,
        'modByConst': args.smt_mod_by_const,
        'expByConst': args.smt_exp_by_const,
        'timeout': args.solver_timeout_branching,
        'sym_jump': args.symbolic_jump,
    }

def main() -> int:
    main_start = timer()

    #
    # z3 global options
    #

    set_option(max_width=240)
    set_option(max_lines=10**8)
    # set_option(max_depth=1000)

    #
    # command line arguments
    #

    args, halmos_unknown_args = parse_args()

    crytic_compile_parser = mk_crytic_parser()
    if args.help_compile:
        print_help_compile(crytic_compile_parser)
        return 0

    options = mk_options(args)

    if args.width is not None:
        options['max_width'] = args.width

    if args.depth is not None:
        options['max_depth'] = args.depth

    if args.loop is not None:
        options['max_loop'] = args.loop

    arrlen = {}
    if args.array_lengths:
        for assign in [x.split('=') for x in args.array_lengths.split(',')]:
            name = assign[0].strip()
            size = assign[1].strip()
            arrlen[name] = int(size)

    # quick bytecode execution mode
    if args.bytecode is not None:
        run_bytecode(args.bytecode, args, options)
        return 0

    #
    # compile
    #

    try:
        crytic_compile_args, crytic_compile_unknown_args  = crytic_compile_parser.parse_known_args()

        both_unknown = set(halmos_unknown_args) & set(crytic_compile_unknown_args)
        if both_unknown:
            print(color_warn(f'error: unrecognized arguments: {" ".join(both_unknown)}'))
            return 1

        cryticCompile = CryticCompile(target=args.root, **vars(crytic_compile_args))
    except InvalidCompilation as e:
        print(color_warn(f'Parse error: {e}'))
        return 1

    main_mid = timer()

    #
    # run
    #

    total_passed = 0
    total_failed = 0

    for compilation_id, compilation_unit in cryticCompile.compilation_units.items():

        for filename in sorted(compilation_unit.filenames):
            contracts_names = compilation_unit.filename_to_contracts[filename]
            source_unit = compilation_unit.source_units[filename]

            if args.contract:
                if args.contract not in contracts_names: continue
                contracts = [args.contract]
            else:
                contracts = sorted(contracts_names)

            for contract in contracts:
                contract_start = timer()

                hexcode = source_unit.bytecodes_runtime[contract]
                abi = source_unit.abis[contract]
                methodIdentifiers = source_unit.hashes(contract)

                funsigs = [funsig for funsig in methodIdentifiers if funsig.startswith(args.function)]

                if funsigs:
                    num_passed = 0
                    num_failed = 0
                    print(f'\nRunning {len(funsigs)} tests for {filename.short}:{contract}')

                    setup_sigs = sorted([ (k,v) for k,v in methodIdentifiers.items() if k == 'setUp()' or k.startswith('setUpSymbolic(') ])
                    (setup_name, setup_sig, setup_selector) = (None, None, None)
                    if len(setup_sigs) > 0:
                        (setup_sig, setup_selector) = setup_sigs[-1]
                        setup_name = setup_sig.split('(')[0]
                        if args.verbose >= 2 or args.debug: print(f'Running {setup_sig}')
                    try:
                        setup_ex = setup(hexcode, abi, setup_name, setup_sig, setup_selector, arrlen, args, options)
                    except Exception as err:
                        print(color_warn(f'Error: {setup_sig} failed: {type(err).__name__}: {err}'))
                        if args.debug: traceback.print_exc()
                        continue

                    if args.reset_bytecode:
                        for assign in [x.split('=') for x in args.reset_bytecode.split(',')]:
                            addr = con(int(assign[0].strip(), 0))
                            new_hexcode = assign[1].strip()
                            (new_pgm, new_code) = decode_hex(new_hexcode)
                            setup_ex.pgm[addr] = new_pgm
                            setup_ex.code[addr] = new_code

                    for funsig in funsigs:
                        funselector = methodIdentifiers[funsig]
                        funname = funsig.split('(')[0]
                        try:
                            exitcode = run(setup_ex, abi, funname, funsig, funselector, arrlen, args, options)
                        except Exception as err:
                            print(f'{color_warn("[SKIP]")} {funsig}')
                            print(color_warn(f'{type(err).__name__}: {err}'))
                            if args.debug: traceback.print_exc()
                            num_failed += 1
                            continue
                        if exitcode == 0:
                            num_passed += 1
                        else:
                            num_failed += 1

                    print(f'Symbolic test result: {num_passed} passed; {num_failed} failed; time: {timer() - contract_start:0.2f}s')
                    total_passed += num_passed
                    total_failed += num_failed

    main_end = timer()

    if args.statistics:
        print(f'\n[time] total: {main_end - main_start:0.2f}s (build: {main_mid - main_start:0.2f}s, tests: {main_end - main_mid:0.2f}s)')

    if (total_passed + total_failed) == 0:
        error_msg = f'Error: No tests with the prefix `{args.function}`'
        if args.contract is not None:
            error_msg += f' in {args.contract}'
        print(color_warn(error_msg))
        return 1

    # exitcode
    if total_failed == 0:
        return 0
    else:
        return 1

if __name__ == '__main__':
    sys.exit(main())
